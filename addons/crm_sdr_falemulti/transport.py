from dataclasses import dataclass


@dataclass(frozen=True)
class Delivery:
    state: str
    external_id: str = ""


class DisabledTransport:
    """No network path exists until a provider contract is homologated."""
    def snapshot(self, conversation):
        return None

    def send(self, conversation, *, key, body, template):
        return Delivery("rejected")

    def reconcile(self, conversation, key):
        return Delivery("uncertain")


class SimulatedTransport(DisabledTransport):
    def __init__(self, snapshots, deliveries=None):
        self.snapshots = snapshots
        self.deliveries = deliveries if deliveries is not None else {}

    def snapshot(self, conversation):
        return self.snapshots.get(conversation)

    def send(self, conversation, *, key, body, template):
        return self.deliveries.setdefault(key, Delivery("confirmed", "sim:" + key))

    def reconcile(self, conversation, key):
        return self.deliveries.get(key, Delivery("uncertain"))
