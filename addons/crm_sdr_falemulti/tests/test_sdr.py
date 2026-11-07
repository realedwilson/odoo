from datetime import datetime, timedelta

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import new_test_user

from ..transport import DisabledTransport


@tagged("post_install", "-at_install")
class TestSdr(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(cls.env, login="sdr_test_operator",
                                     groups="crm_sdr_falemulti.group_operator")
        cls.supervisor = new_test_user(cls.env, login="sdr_test_supervisor",
                                       groups="crm_sdr_falemulti.group_supervisor")
        cls.queue = cls.env["sdr.fm.queue"].create({
            "name": "Piloto", "account": "test", "channel": "wa", "external_queue": "1",
            "operator_ids": [(4, cls.operator.id)],
        })
        cls.campaign = cls.env["sdr.fm.campaign"].create({
            "name": "Reativação", "queue_id": cls.queue.id, "paused": False, "template": "approved",
        })
        cls.lead = cls.env["crm.lead"].create({"name": "Lead sintético", "company_id": cls.env.company.id})
        cls.conversation = cls.env["sdr.fm.conversation"].create({
            "lead_id": cls.lead.id, "queue_id": cls.queue.id, "campaign_id": cls.campaign.id,
            "owner_id": cls.operator.id, "external_contact": "synthetic", "ticket": "ticket-1",
            "eligible": True,
        })
        cls.conversation.with_user(cls.supervisor).action_resume()
        cls.now = datetime(2026, 11, 6, 15)

    def _intent(self, **values):
        return self.env["sdr.fm.intent"].create({
            "conversation_id": self.conversation.id, "body": "Olá", **values,
        })

    def test_reply_deduplicates_and_invalidates_approval(self):
        row = self._intent()
        row.with_user(self.supervisor).action_approve()
        event = self.conversation._record_event("reply-1", "reply", self.now)
        revision = self.conversation.revision
        self.assertEqual(row.state, "cancelled")
        self.assertEqual(event, self.conversation._record_event("reply-1", "reply", self.now))
        self.assertEqual(self.conversation.revision, revision)
        self.conversation._record_event("reply-older", "reply", self.now - timedelta(days=1))
        self.assertEqual(self.conversation.last_event_at, self.now)

    def test_operator_cannot_approve_and_other_queue_cannot_read(self):
        row = self._intent().with_user(self.operator)
        with self.assertRaises(AccessError):
            row.action_approve()
        outsider = new_test_user(self.env, login="sdr_outsider",
                                 groups="crm_sdr_falemulti.group_operator")
        self.assertFalse(self.env["sdr.fm.intent"].with_user(outsider).search([("id", "=", row.id)]))

    def test_template_and_external_takeover(self):
        row = self._intent(template="invalid", body=False)
        row.with_user(self.supervisor).action_approve()
        snapshot = {
            "identity": self.conversation._transport_identity(),
            "revision": self.conversation.revision,
            "human": False, "opt_out": False, "templates": ["approved"],
        }
        self.assertEqual(row._block_reason(snapshot, self.now), "template_not_approved")
        self.assertEqual(row._block_reason(dict(snapshot, human=True), self.now), "external_block")
        self.conversation.with_user(self.operator).action_assume()
        self.assertEqual(row.state, "cancelled")

    def test_opt_out_is_terminal(self):
        self.conversation._record_event("stop", "opt_out", self.now)
        self.conversation.with_user(self.supervisor).action_resume()
        self.assertEqual(self.conversation.state, "opt_out")

    def test_human_correction_wins(self):
        event = self.conversation._record_event("reply-2", "reply", self.now)
        self.env["sdr.fm.fact"].create({
            "conversation_id": self.conversation.id, "key": "prazo", "value": "março",
            "evidence": "revisão humana", "status": "corrected",
        })
        self.conversation._qualify(event, "abril", lambda *_: {
            "facts": {"prazo": "abril"}, "handoff": False,
        })
        self.assertEqual(self.conversation._memory()["prazo"], "março")
        self.assertTrue(event.qualified)

    def test_abandoned_delivery_is_uncertain_and_never_requeued(self):
        row = self._intent()
        row.sudo().write({"state": "sending", "reserved_at": self.now - timedelta(days=365)})
        row._recover_expired()
        self.assertEqual(row.state, "uncertain")
        row._reconcile(DisabledTransport())
        self.assertEqual(row.state, "uncertain")
