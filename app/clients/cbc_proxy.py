# Noop = no operation
class CBCProxyNoopClient:

    def init_app(self, app):
        pass

    def create_and_send_broadcast(
        self,
        identifier, headline, description,
    ):
        # identifier=broadcast_message.identifier,
        # headline="GOV.UK Notify Broadcast",
        # description=broadcast_message.description,
        pass

    # We have not implementated updating a broadcast
    def update_and_send_broadcast(
        self,
        identifier, references, headline, description,
    ):
        pass

    # We have not implemented cancelling a broadcast
    def cancel_broadcast(
        self,
        identifier, references, headline, description,
    ):
        pass


class CBCProxyClient:

    def init_app(self, app):
        self.aws_access_key_id = app.config['CBC_PROXY_AWS_ACCESS_KEY_ID']
        self.aws_secret_access_key = app.config['CBC_PROXY_AWS_SECRET_ACCESS_KEY']
        self.aws_region = 'eu-west-2'

    def create_and_send_broadcast(
        self,
        identifier, headline, description,
    ):
        # identifier=broadcast_message.identifier,
        # headline="GOV.UK Notify Broadcast",
        # description=broadcast_message.description,
        pass

    # We have not implementated updating a broadcast
    def update_and_send_broadcast(
        self,
        identifier, references, headline, description,
    ):
        pass

    # We have not implemented cancelling a broadcast
    def cancel_broadcast(
        self,
        identifier, references, headline, description,
    ):
        pass
