from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .. import qualifier


class Lead(models.Model):
    _inherit = "crm.lead"

    sdr_native_ids = fields.One2many("sdr.native.intake", "lead_id",
                                    groups="crm_sdr_falemulti.group_operator")


class Intake(models.Model):
    _name = "sdr.native.intake"
    _description = "Interação de qualificação no Odoo"
    _order = "id desc"
    _rec_name = "lead_id"
    _check_company_auto = True

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade", index=True, check_company=True)
    company_id = fields.Many2one(related="lead_id.company_id", store=True, index=True)
    owner_id = fields.Many2one(related="lead_id.user_id", store=True, index=True)
    text = fields.Text(required=True)
    state = fields.Selection([
        ("queued", "Na fila"), ("done", "Analisada"), ("review", "Revisão humana"),
    ], default="queued", readonly=True, required=True, index=True)
    facts = fields.Json(readonly=True)
    correction = fields.Json(readonly=True)
    next_step = fields.Char(readonly=True)
    failure = fields.Char(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        clean = []
        for vals in vals_list:
            lead = self.env["crm.lead"].browse(vals.get("lead_id")).exists()
            if not lead or not lead.company_id or not lead.user_id:
                raise ValidationError("Selecione uma oportunidade com empresa e vendedor responsável.")
            lead.check_access("write")
            text = vals.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2500:
                raise ValidationError("Registre de 1 a 2.500 caracteres.")
            clean.append({"lead_id": lead.id, "text": text.strip()})
        return super().create(clean)

    def write(self, vals):
        if not self.env.su:
            raise AccessError("A interação original é imutável. Use a ação de corrigir.")
        return super().write(vals)

    def _history(self):
        self.ensure_one()
        rows = self.sudo().search([("lead_id", "=", self.lead_id.id), ("id", "<=", self.id)],
                                  order="id asc")
        facts, corrected = {}, {}
        for row in rows:
            facts.update(row.facts or {})
            corrected.update(row.correction or {})
        facts.update(corrected)
        return facts

    def _suggest_improvement(self, failure):
        self.ensure_one()
        if failure != "Nenhum fato novo verificável.":
            return
        cutoff = fields.Datetime.now() - timedelta(days=14)
        count = self.sudo().search_count([
            ("company_id", "=", self.company_id.id), ("failure", "=", failure),
            ("create_date", ">=", cutoff),
        ])
        reviews = self.env["sdr.native.review"].sudo().with_context(active_test=False)
        if count < 3 or reviews.search_count([
            ("company_id", "=", self.company_id.id), ("source_failure", "=", "no_evidence"),
        ]):
            return
        reviews.create({
            "company_id": self.company_id.id, "source_failure": "no_evidence",
            "name": "Sugestão: reconhecer ausência de evidência",
            "guidance": "Separe declarações do cliente de perguntas. Se não houver fato explícito, retorne facts vazio.",
            "evidence": f"{count} interações sem fato verificável nos últimos 14 dias. Avaliação humana pendente.",
        })

    def _evaluate(self, extract=qualifier.extract):
        self.ensure_one()
        if self.state != "queued":
            return
        memory = self._history()
        feedback = self.env["sdr.native.review"].sudo().search([
            ("company_id", "=", self.company_id.id), ("active", "=", True),
        ], limit=1)
        try:
            facts, handoff = qualifier.validate(extract(self.text, memory, feedback.guidance or ""), self.text)
        except Exception:
            self.write({"state": "review", "failure": "Modelo indisponível ou saída inválida.",
                        "next_step": "Qualificar manualmente."})
            return
        question = qualifier.next_question({**memory, **facts})
        needs_review = handoff or not facts or not question
        self.write({
            "state": "review" if needs_review else "done", "facts": facts,
            "failure": "Nenhum fato novo verificável." if not facts else False,
            "next_step": "Vendedor: revisar e assumir." if needs_review else question,
        })
        if self.failure:
            self._suggest_improvement(self.failure)
        if needs_review and self.owner_id:
            self.lead_id.activity_schedule(
                "mail.mail_activity_data_todo", user_id=self.owner_id.id,
                summary="Revisar qualificação SDR",
            )

    @api.model
    def _cron_qualify(self):
        self.env.cr.execute("""
            SELECT item.id FROM sdr_native_intake item
            WHERE item.state = 'queued' AND NOT EXISTS (
                SELECT 1 FROM sdr_native_intake earlier
                WHERE earlier.lead_id = item.lead_id AND earlier.state = 'queued' AND earlier.id < item.id
            )
            ORDER BY item.id LIMIT 1 FOR UPDATE OF item SKIP LOCKED
        """)
        row = self.env.cr.fetchone()
        if row:
            self.sudo().browse(row[0])._evaluate()
        self.env["ir.cron"]._commit_progress(1 if row else 0, remaining=0)

    def action_correct(self):
        self.ensure_one()
        self.check_access("read")
        self.lead_id.check_access("write")
        return {
            "type": "ir.actions.act_window", "name": "Corrigir qualificação",
            "res_model": "sdr.native.correction", "view_mode": "form", "target": "new",
            "context": {"default_intake_id": self.id},
        }


class Correction(models.TransientModel):
    _name = "sdr.native.correction"
    _description = "Correção humana de fato SDR"

    intake_id = fields.Many2one("sdr.native.intake", required=True)
    field = fields.Selection([(key, key.capitalize()) for key in qualifier.QUESTIONS], required=True)
    value = fields.Char(required=True)

    def action_save(self):
        self.ensure_one()
        self.intake_id.check_access("read")
        self.intake_id.lead_id.check_access("write")
        if not self.value.strip() or len(self.value) > 300:
            raise ValidationError("Informe um fato de até 300 caracteres.")
        row = self.intake_id.sudo()
        self.env.cr.execute("SELECT id FROM sdr_native_intake WHERE id = %s FOR UPDATE", [row.id])
        row.invalidate_recordset()
        row.write({"correction": {**(row.correction or {}), self.field: self.value.strip()}})
        return {"type": "ir.actions.act_window_close"}


class Review(models.Model):
    _name = "sdr.native.review"
    _description = "Versão aprovada de melhoria SDR"

    name = fields.Char(required=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    guidance = fields.Text(required=True)
    evidence = fields.Text(required=True)
    active = fields.Boolean(default=False, readonly=True, copy=False)
    published = fields.Boolean(readonly=True, copy=False)
    source_failure = fields.Char(readonly=True, copy=False)
    evaluation = fields.Text()
    _one_active = models.UniqueIndex("(company_id) WHERE active")
    _one_suggestion = models.Constraint("UNIQUE(company_id, source_failure)", "Sugestão já registrada.")

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([dict(vals, active=False, published=False) for vals in vals_list])

    def write(self, vals):
        if not self.env.su and ({"active", "published"}.intersection(vals) or any(self.mapped("published"))):
            raise AccessError("Use a ação de publicação; versões publicadas são imutáveis.")
        return super().write(vals)

    def action_publish(self):
        self.ensure_one()
        self.check_access("write")
        if not self.env.user.has_group("crm_sdr_falemulti.group_supervisor"):
            raise AccessError("Publicação reservada à supervisão.")
        if not self.evaluation or not self.evaluation.strip():
            raise ValidationError("Registre a avaliação antes de publicar.")
        self.env.cr.execute("SELECT id FROM res_company WHERE id = %s FOR UPDATE", [self.company_id.id])
        self.with_context(active_test=False).sudo().search([
            ("company_id", "=", self.company_id.id), ("active", "=", True),
        ]).write({"active": False})
        self.sudo().write({"active": True, "published": True})
