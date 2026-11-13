from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.exceptions import PermissionDenied


class ClientBoundOAuth2Authentication(OAuth2Authentication):
    """
    OAuth2 authentication that additionally binds the request to the client
    that issued the access token.

    If the request carries an ``X-Client-ID`` header it MUST equal the
    ``client_id`` of the token's application. This closes the class of
    cross-tenant (IDOR) bugs where a holder of any valid access token could
    read or mutate another tenant's data by spoofing the ``X-Client-ID``
    header, since the token's application is the authoritative tenant identity.

    Behaviour:
      * No ``X-Client-ID`` header            -> allowed (identity = token user).
      * Header matches token's application   -> allowed.
      * Header does not match                -> 403 Forbidden.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        _user, token = result

        header_client_id = request.META.get("HTTP_X_CLIENT_ID")
        if header_client_id:
            application = getattr(token, "application", None)
            if application is None or application.client_id != header_client_id:
                raise PermissionDenied(
                    "X-Client-ID does not match the access token's client"
                )

        return result
