import logging
import unicodedata
from datetime import timedelta
from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..transport import DisabledTransport
from .. import qualifier

_logger = logging.getLogger(__name__)


class Scoped(models.AbstractModel):
    _name = "sdr.fm.scope"
    _description = "Escopo SDR"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    queue_id = fields.Many2one("sdr.fm.queue", required=True, check_company=True, index=True)

    def _supervisor(self):
        if not self.env.user.has_group("crm_sdr_falemulti.group_supervisor"):
            raise AccessError("Ação reservada à supervisão SDR.")


class Queue(models.Model):
    _name = "sdr.fm.queue"
    _description = "Fila FaleMulti homologável"

    name = fields.Char(required=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    account = fields.Char(required=True)
    channel = fields.Char(required=True)
    external_queue = fields.Char(required=True)
    operator_ids = fields.Many2many("res.users", string="Operadores")
    _scope_unique = models.Constraint(
        "UNIQUE(company_id, account, channel, external_queue)", "Fila externa já cadastrada."
    )

    def write(self, vals):
        if {"company_id", "account", "channel", "external_queue"}.intersection(vals):
            raise ValidationError("Cadastre outra fila para mudar a identidade externa.")
        return super().write(vals)


class Campaign(models.Model):
    _name = "sdr.fm.campaign"
    _inherit = "sdr.fm.scope"
    _description = "Campanha SDR"

    name = fields.Char(required=True)
    paused = fields.Boolean(default=True)
    daily_limit = fields.Integer(default=10, required=True)
    template = fields.Char(help="Identificador do template aprovado para esta campanha.")
    followup_days = fields.Integer(default=3)
    model_enabled = fields.Boolean(default=False)
    daily_model_calls = fields.Integer(default=20)

    @api.constrains("followup_days", "daily_model_calls")
    def _check_controls(self):
        if any(not 1 <= row.followup_days <= 30 or not 1 <= row.daily_model_calls <= 100 for row in self):
            raise ValidationError("Use de 1 a 30 dias e de 1 a 100 consultas de IA por dia.")
    policy_version = fields.Integer(default=1, required=True, readonly=True)
    policy_id = fields.Many2one("sdr.fm.policy", readonly=True, copy=False)

    def write(self, vals):
        if not self.env.su and {"policy_version", "policy_id"}.intersection(vals):
            raise AccessError("Publique uma versão revisada da política.")
        return super().write(vals)

    @api.constrains("daily_limit")
    def _check_limit(self):
        if any(not 1 <= row.daily_limit <= 10 for row in self):
            raise ValidationError("O piloto admite de 1 a 10 novos contatos por campanha e dia.")

    def action_pause(self):
        self._supervisor()
        self.write({"paused": True})


class Lead(models.Model):
    _inherit = "crm.lead"

    sdr_conversation_ids = fields.One2many("sdr.fm.conversation", "lead_id", groups="crm_sdr_falemulti.group_operator")


class Conversation(models.Model):
    _name = "sdr.fm.conversation"
    _inherit = "sdr.fm.scope"
    _description = "Vínculo SDR e atendimento"

    lead_id = fields.Many2one("crm.lead", required=True, check_company=True, ondelete="restrict")
    partner_id = fields.Many2one(related="lead_id.partner_id")
    campaign_id = fields.Many2one("sdr.fm.campaign", required=True, check_company=True, ondelete="restrict")
    external_contact = fields.Char(required=True)
    ticket = fields.Char(required=True)
    ticket_url = fields.Char()

    @api.constrains("ticket_url")
    def _check_ticket_url(self):
        for row in self:
            if row.ticket_url and not row.ticket_url.startswith("https://"):
                raise ValidationError("O link do ticket deve usar HTTPS.")
    owner_id = fields.Many2one("res.users", required=True)
    eligible = fields.Boolean(help="Autorização de contato confirmada pela supervisão.")
    state = fields.Selection([(v, v) for v in ("paused", "active", "human", "opt_out")],
                             required=True, default="paused")
    revision = fields.Integer(default=0, required=True)
    last_event_at = fields.Datetime()
    next_step = fields.Char(readonly=True)
    fact_ids = fields.One2many("sdr.fm.fact", "conversation_id")
    account = fields.Char(related="queue_id.account", store=True, index=True)
    channel = fields.Char(related="queue_id.channel", store=True)
    _ticket_unique = models.Constraint(
        "UNIQUE(company_id, account, channel, ticket)", "Ticket já associado nesta conta e canal."
    )

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([dict(vals, state="paused", revision=0, last_event_at=False, next_step=False) for vals in vals_list])

    def _transport_identity(self):
        self.ensure_one()
        return (self.account, self.channel, self.queue_id.external_queue, self.ticket, self.external_contact)

    def _ingest_verified(self, external_id, kind, occurred_at, text=None):
        """Private adapter boundary. Call only after authenticating the source."""
        self.ensure_one()
        if kind == "reply" and text:
            normal = "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                             if not unicodedata.combining(c)).strip(" .!?")
            if normal in {"parar", "sair", "stop", "cancelar", "descadastrar",
                          "nao me mande mais mensagens", "nao quero receber mensagens"}:
                kind = "opt_out"
        event = self._record_event(external_id, kind, occurred_at)
        if kind == "reply" and text and not event.qualified and self.campaign_id.model_enabled:
            self._qualify(event, text)
        return event

    @api.constrains("campaign_id", "queue_id", "owner_id", "lead_id")
    def _check_scope(self):
        for row in self:
            if (row.campaign_id.queue_id != row.queue_id
                    or row.owner_id not in row.queue_id.operator_ids
                    or row.lead_id.company_id != row.company_id):
                raise ValidationError("Campanha, responsável e oportunidade precisam pertencer ao escopo do vínculo.")

    def _transition(self, state):
        for row in self:
            row._lock()
            if row.state == "opt_out":
                continue
            row.sudo().write({"state": state, "revision": row.revision + 1})
            row._cancel_pending()

    def _lock(self):
        self.ensure_one()
        self.env.cr.execute("SELECT id FROM sdr_fm_conversation WHERE id = %s FOR UPDATE", [self.id])
        self.invalidate_recordset()

    def _cancel_pending(self):
        self.env["sdr.fm.intent"].sudo().search([
            ("conversation_id", "=", self.id), ("state", "in", ["review", "ready", "reserved"]),
        ]).write({"state": "cancelled"})

    def action_assume(self):
        self.check_access("read")
        self._transition("human")

    def action_pause(self):
        self.check_access("read")
        self._transition("paused")

    def action_resume(self):
        self._supervisor()
        self.check_access("read")
        self._transition("active")

    def _record_event(self, external_id, kind, occurred_at):
        self.ensure_one()
        self._lock()
        events = self.env["sdr.fm.event"].sudo()
        existing = events.search([("conversation_id", "=", self.id), ("external_id", "=", external_id)], limit=1)
        if existing:
            return existing
        event = events.create(dict(conversation_id=self.id, external_id=external_id,
                                   kind=kind, occurred_at=occurred_at))
        values = {"revision": self.revision + 1}
        if not self.last_event_at or event.occurred_at > self.last_event_at:
            values["last_event_at"] = event.occurred_at
        if kind == "opt_out":
            values["state"] = "opt_out"
        elif kind == "human" and self.state != "opt_out":
            values["state"] = "human"
        self.sudo().write(values)
        self._cancel_pending()
        return event

    def _qualify(self, event, text, extractor=qualifier.extract):
        self.ensure_one()
        if event.conversation_id != self or event.kind != "reply":
            raise ValidationError("Evento incompatível com a conversa.")
        if not isinstance(text, str) or not 1 <= len(text) <= 4000:
            raise ValidationError("Texto inválido para qualificação.")
        self._lock()
        if self.state != "active" or event.occurred_at != self.last_event_at or event.qualified:
            return
        revision = self.revision
        campaign = self.campaign_id
        self.env.cr.execute("SELECT id FROM sdr_fm_campaign WHERE id = %s FOR UPDATE", [campaign.id])
        campaign.invalidate_recordset()
        if campaign.paused:
            return
        now = fields.Datetime.now()
        used = self.env["sdr.fm.event"].sudo().search_count([
            ("conversation_id.campaign_id", "=", campaign.id),
            ("model_attempt_at", ">=", now.replace(hour=0, minute=0, second=0, microsecond=0)),
        ])
        if used >= campaign.daily_model_calls:
            self._transition("human")
            self.sudo().write({"next_step": "Limite de consultas da IA atingido; revisar manualmente."})
            return
        event.sudo().write({"model_attempt_at": now})
        try:
            facts, handoff = qualifier.validate(
                extractor(text, self._memory(), self.campaign_id.policy_id.guidance or ""), text
            )
        except Exception:
            self.env["sdr.fm.incident"].sudo().create({
                "conversation_id": self.id, "category": "qualification_failure",
                "policy_version": self.campaign_id.policy_version, "event_id": event.id,
            })
            self._transition("human")
            return
        for key, value in facts.items():
            previous = self.fact_ids.filtered(lambda fact: fact.key == key)
            if previous and previous.status in {"corrected", "confirmed", "discarded"}:
                continue
            values = dict(value=value, evidence=event.external_id, status="informed")
            if previous:
                previous.sudo().write(values)
            else:
                self.env["sdr.fm.fact"].sudo().create(dict(values, conversation_id=self.id, key=key))
        event.sudo().write({"qualified": True})
        question = qualifier.next_question(self._memory())
        if handoff or not question:
            self._transition("human")
            self.sudo().write({"next_step": "Revisar qualificação e assumir pelo FaleMulti."})
            self.lead_id.sudo().activity_schedule(
                "mail.mail_activity_data_todo", user_id=self.owner_id.id,
                summary="Revisar qualificação SDR no FaleMulti",
            )
        else:
            self.sudo().write({"next_step": question})
            self.env["sdr.fm.intent"].sudo().create({
                "conversation_id": self.id, "cycle": revision + 1, "step": 0, "body": question,
            })

    def _memory(self):
        self.ensure_one()
        return {fact.key: fact.value for fact in self.fact_ids if fact.status != "discarded"}


class Child(models.AbstractModel):
    _name = "sdr.fm.child"
    _description = "Registro vinculado SDR"
    _check_company_auto = True

    conversation_id = fields.Many2one("sdr.fm.conversation", required=True, ondelete="restrict", index=True)
    company_id = fields.Many2one(related="conversation_id.company_id", store=True, index=True)
    queue_id = fields.Many2one(related="conversation_id.queue_id", store=True, index=True)


class Event(models.Model):
    _name = "sdr.fm.event"
    _inherit = "sdr.fm.child"
    _description = "Evento verificado SDR"

    external_id = fields.Char(required=True)
    kind = fields.Selection([(v, v) for v in ("reply", "human", "opt_out")], required=True)
    occurred_at = fields.Datetime(required=True)
    qualified = fields.Boolean(readonly=True)
    model_attempt_at = fields.Datetime(readonly=True, index=True)
    _event_unique = models.Constraint("UNIQUE(conversation_id, external_id)", "Evento já recebido.")


class Fact(models.Model):
    _name = "sdr.fm.fact"
    _inherit = "sdr.fm.child"
    _description = "Memória comercial SDR"

    key = fields.Selection([(v, v) for v in ("problema", "operacao", "papel", "prazo")], required=True)
    value = fields.Char(required=True)
    evidence = fields.Char(required=True)
    status = fields.Selection([(v, v) for v in ("informed", "confirmed", "corrected", "discarded")],
                              default="informed", required=True)
    def write(self, vals):
        if {"conversation_id", "key"}.intersection(vals):
            raise ValidationError("A identidade do fato não pode ser alterada.")
        if not self.env.su:
            self.check_access("write")
            for conversation in self.mapped("conversation_id").sorted("id"):
                conversation._lock()
                conversation.sudo().write({"revision": conversation.revision + 1})
                conversation._cancel_pending()
        return super().write(vals)

    _key_unique = models.Constraint("UNIQUE(conversation_id, key)", "Já existe um fato para este campo.")


class Intent(models.Model):
    _name = "sdr.fm.intent"
    _inherit = "sdr.fm.child"
    _description = "Fila e auditoria de envios SDR"
    _order = "due_at, id"

    cycle = fields.Integer(required=True, default=1)
    step = fields.Integer(required=True, default=1)
    body = fields.Text()
    template = fields.Char()
    due_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    revision = fields.Integer(readonly=True)
    policy_version = fields.Integer(readonly=True)
    reserved_at = fields.Datetime(readonly=True)
    started_at = fields.Datetime(readonly=True, index=True)
    external_id = fields.Char(readonly=True)
    reason = fields.Char(readonly=True)
    state = fields.Selection([(v, v) for v in (
        "review", "ready", "reserved", "sending", "confirmed", "rejected", "uncertain", "cancelled"
    )], default="review", required=True, index=True)
    _intent_unique = models.Constraint("UNIQUE(conversation_id, cycle, step)", "Etapa já existe neste ciclo.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.update(state="review", reserved_at=False, started_at=False, external_id=False, reason=False)
            if not self.env.su and (vals.get("step", 1) < 1 or vals.get("cycle", 1) < 1):
                raise ValidationError("Ciclo e etapa manuais precisam ser positivos.")
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self.check_access("write")
            if self.ids:
                self.env.cr.execute(
                    "SELECT id FROM sdr_fm_intent WHERE id IN %s ORDER BY id FOR UPDATE",
                    [tuple(self.ids)],
                )
                self.invalidate_recordset()
            if set(vals) - {"body", "template", "due_at"} or any(row.state != "review" for row in self):
                raise AccessError("Somente rascunhos podem ser editados.")
        return super().write(vals)

    def action_approve(self):
        self.check_access("write")
        if not self.env.user.has_group("crm_sdr_falemulti.group_supervisor"):
            raise AccessError("Aprovação reservada à supervisão.")
        for row in self:
            conv = row.conversation_id
            conv._lock()
            self.env.cr.execute("SELECT id FROM sdr_fm_intent WHERE id = %s FOR UPDATE", [row.id])
            row.invalidate_recordset()
            if row.state != "review" or bool(row.body) == bool(row.template):
                raise ValidationError("Aprove somente um rascunho com texto ou template, sem combinar os dois.")
            row.sudo().write(dict(state="ready", revision=conv.revision,
                                  policy_version=conv.campaign_id.policy_version))

    def _transport(self):
        return DisabledTransport()

    def _delivery_key(self):
        return f"sdr:{self.company_id.id}:{self.conversation_id.id}:{self.cycle}:{self.step}"

    def _block_reason(self, snapshot, now):
        conv = self.conversation_id
        campaign = conv.campaign_id
        if campaign.paused or conv.state != "active" or not conv.eligible or conv.owner_id not in conv.queue_id.operator_ids:
            return "contact_or_campaign_blocked"
        if self.env["sdr.fm.conversation"].sudo().search_count([
            ("company_id", "=", conv.company_id.id), ("account", "=", conv.account),
            ("external_contact", "=", conv.external_contact), ("state", "=", "opt_out"),
        ], limit=1):
            return "contact_or_campaign_blocked"
        if self.revision != conv.revision or self.policy_version != campaign.policy_version:
            return "stale_review"
        local = fields.Datetime.context_timestamp(self.with_context(tz="America/Sao_Paulo"), now)
        if local.weekday() >= 5 or not 9 <= local.hour < 18:
            return "outside_business_hours"
        if not snapshot or snapshot.get("identity") != conv._transport_identity() or snapshot.get("revision") != conv.revision:
            return "unverified_ticket"
        if snapshot.get("human") is not False or snapshot.get("opt_out") is not False:
            return "external_block"
        if self.template:
            if self.body or self.template != campaign.template or self.template not in snapshot.get("templates", ()):
                return "template_not_approved"
        elif not self.body or not snapshot.get("window_until") or snapshot["window_until"] <= now:
            return "outside_window"
        return False

    @api.model
    def _recover_expired(self):
        self.flush_model(["state", "reserved_at"])
        cutoff = fields.Datetime.now() - timedelta(minutes=10)
        for state, target in (("reserved", "cancelled"), ("sending", "uncertain")):
            self.env.cr.execute("""
                SELECT id FROM sdr_fm_intent WHERE state = %s AND reserved_at < %s
                ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED
            """, [state, cutoff])
            self.sudo().browse([row[0] for row in self.env.cr.fetchall()]).write({
                "state": target, "reason": "expired_reservation",
            })

    def _reconcile(self, transport):
        for row in self.filtered(lambda item: item.state == "uncertain"):
            result = transport.reconcile(row.conversation_id._transport_identity(), row._delivery_key())
            if result.state == "confirmed" and result.external_id:
                row.sudo().write({"state": "confirmed", "external_id": result.external_id})
            elif result.state == "rejected":
                row.sudo().write({"state": "rejected"})

    @api.model
    def _cron_dispatch(self):
        # Safe on fresh installations and whenever the real connector is unavailable.
        self._recover_expired()
        transport = self._transport()
        if type(transport) is not DisabledTransport:
            self._dispatch_batch(transport)

    def _dispatch_batch(self, transport, limit=5):
        if type(transport) is DisabledTransport:
            return
        self.flush_model(["state", "due_at"])
        now = fields.Datetime.now()
        self.env.cr.execute("""
            SELECT id FROM sdr_fm_intent
            WHERE state = 'ready' AND due_at <= %s ORDER BY due_at, id
            LIMIT %s FOR UPDATE SKIP LOCKED
        """, [now, min(max(limit, 1), 5)])
        batch = self.sudo().browse([row[0] for row in self.env.cr.fetchall()])
        batch.write({"state": "reserved", "reserved_at": now})
        self.env.cr.commit()
        for intent in batch:
            conv = intent.conversation_id
            try:
                snapshot = transport.snapshot(conv._transport_identity())
            except Exception:
                snapshot = None
            conv._lock()
            intent.invalidate_recordset()
            campaign = conv.campaign_id
            self.env.cr.execute("SELECT id FROM sdr_fm_campaign WHERE id = %s FOR UPDATE", [campaign.id])
            campaign.invalidate_recordset()
            if intent.state != "reserved":
                self.env.cr.commit()
                continue
            reason = intent._block_reason(snapshot, fields.Datetime.now())
            if reason:
                intent.write({"state": "cancelled", "reason": reason})
                self.env.cr.commit()
                continue
            if intent.step == 1:
                # Serialize the daily quota across campaigns in the same company.
                self.env.cr.execute("SELECT id FROM res_company WHERE id = %s FOR UPDATE", [conv.company_id.id])
                local = fields.Datetime.context_timestamp(intent.with_context(tz="America/Sao_Paulo"), fields.Datetime.now())
                start = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                         .astimezone(ZoneInfo("UTC")).replace(tzinfo=None))
                used = self.sudo().search_count([
                    ("company_id", "=", conv.company_id.id), ("started_at", ">=", start), ("step", "=", 1),
                ])
                if used >= campaign.daily_limit:
                    intent.write({"state": "cancelled", "reason": "daily_limit"})
                    self.env.cr.commit()
                    continue
            key, body, template = intent._delivery_key(), intent.body, intent.template
            identity = conv._transport_identity()
            intent.write({"state": "sending", "started_at": fields.Datetime.now()})
            self.env.cr.commit()
            try:
                result = transport.send(identity, key=key, body=body, template=template)
                state = result.state if result.state in {"confirmed", "rejected", "uncertain"} else "uncertain"
                if state == "confirmed" and not result.external_id:
                    state = "uncertain"
                intent.write({"state": state, "external_id": result.external_id})
            except Exception:
                _logger.warning("Resultado de envio SDR incerto: intenção %s", intent.id)
                intent.write({"state": "uncertain", "reason": "transport_exception"})
            self.env.cr.commit()
            if intent.state == "confirmed" and intent.step == 1:
                intent._draft_followup()
                self.env.cr.commit()

    def _draft_followup(self):
        self.ensure_one()
        conv = self.conversation_id
        conv._lock()
        campaign = conv.campaign_id
        if (conv.revision != self.revision or campaign.policy_version != self.policy_version
                or conv.state != "active" or campaign.paused or not campaign.template):
            return
        if not self.search_count([
            ("conversation_id", "=", conv.id), ("cycle", "=", self.cycle), ("step", "=", 2),
        ]):
            self.create({
                "conversation_id": conv.id, "cycle": self.cycle, "step": 2,
                "template": campaign.template,
                "due_at": self.started_at + timedelta(days=campaign.followup_days),
            })


class Policy(models.Model):
    _name = "sdr.fm.policy"
    _inherit = "sdr.fm.scope"
    _description = "Versão revisada de comportamento SDR"

    name = fields.Char(required=True)
    campaign_id = fields.Many2one("sdr.fm.campaign", required=True, check_company=True)
    guidance = fields.Text(required=True)
    evaluation = fields.Text()
    published = fields.Boolean(readonly=True, copy=False)

    @api.constrains("campaign_id", "queue_id")
    def _check_queue(self):
        if any(row.queue_id != row.campaign_id.queue_id for row in self):
            raise ValidationError("A política deve usar a fila da campanha.")

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([dict(vals, published=False) for vals in vals_list])

    def write(self, vals):
        if not self.env.su and ("published" in vals or any(self.mapped("published"))):
            raise AccessError("Uma versão publicada é imutável.")
        return super().write(vals)

    def action_publish(self):
        self._supervisor()
        self.check_access("write")
        for row in self:
            if not row.evaluation:
                raise ValidationError("Registre a avaliação comparativa antes de publicar.")
            campaign = row.campaign_id
            self.env.cr.execute("SELECT id FROM sdr_fm_campaign WHERE id = %s FOR UPDATE", [campaign.id])
            campaign.invalidate_recordset()
            campaign.sudo().write({"policy_id": row.id, "policy_version": campaign.policy_version + 1})
            row.sudo().write({"published": True})


class Incident(models.Model):
    _name = "sdr.fm.incident"
    _inherit = "sdr.fm.child"
    _description = "Falha observada no SDR"

    category = fields.Char(required=True)
    event_id = fields.Many2one("sdr.fm.event", ondelete="restrict")
    policy_version = fields.Integer(required=True)
