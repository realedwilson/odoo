from odoo.tests import TransactionCase, tagged

from .. import qualifier


@tagged("post_install", "-at_install")
class TestNativeQualification(TransactionCase):
    def test_memory_scoped_and_correction_precedes_model(self):
        lead = self.env["crm.lead"].create({
            "name": "Teste SDR interno", "company_id": self.env.company.id,
            "user_id": self.env.user.id,
        })
        items = self.env["sdr.native.intake"]
        first = items.create({"lead_id": lead.id, "text": "Queremos começar em março."})
        first.sudo()._evaluate(lambda *_: {"facts": {"prazo": "março"}, "handoff": False})
        self.assertEqual(first.state, "done")
        self.assertEqual(first.facts, {"prazo": "março"})
        wizard = self.env["sdr.native.correction"].create({
            "intake_id": first.id, "field": "prazo", "value": "abril",
        })
        wizard.action_save()
        second = items.create({"lead_id": lead.id, "text": "Hoje usamos planilhas."})

        def fake(text, memory, guidance):
            self.assertEqual(memory, {"prazo": "abril"})
            return {"facts": {"operacao": "planilhas"}, "handoff": False}

        second.sudo()._evaluate(fake)
        self.assertEqual(second._history(), {"prazo": "abril", "operacao": "planilhas"})
        self.assertEqual(second.next_step, qualifier.QUESTIONS["problema"])

    def test_model_failure_requests_human_review(self):
        lead = self.env["crm.lead"].create({
            "name": "Falha simulada", "company_id": self.env.company.id,
            "user_id": self.env.user.id,
        })
        item = self.env["sdr.native.intake"].create({"lead_id": lead.id, "text": "Olá"})

        def unavailable(*args):
            raise TimeoutError

        item.sudo()._evaluate(unavailable)
        self.assertEqual(item.state, "review")
        self.assertTrue(item.failure)
