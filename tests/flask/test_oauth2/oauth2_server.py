import os
import time
import base64
import unittest
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from authlib.common.security import generate_token
from authlib.common.encoding import to_bytes, to_unicode
from authlib.flask.oauth2.sqla import (
    OAuth2ClientMixin,
    OAuth2AuthorizationCodeMixin,
    OAuth2TokenMixin,
    create_bearer_token_validator,
    create_query_client_func,
    create_save_token_func,
)
from authlib.flask.oauth2 import (
    AuthorizationServer,
    ResourceProtector,
    current_token,
)
from authlib.specs.rfc6749 import OAuth2Error
from authlib.specs.rfc6749.grants import (
    AuthorizationCodeGrant as _AuthorizationCodeGrant,
    ResourceOwnerPasswordCredentialsGrant as _PasswordGrant,
    RefreshTokenGrant as _RefreshTokenGrant,
)
from authlib.specs.oidc.grants import (
    OpenIDCodeGrant as _OpenIDCodeGrant
)

os.environ['AUTHLIB_INSECURE_TRANSPORT'] = 'true'
db = SQLAlchemy()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)

    def get_user_id(self):
        return self.id

    def check_password(self, password):
        return password != 'wrong'

    def generate_openid_claims(self, claims):
        profile = {'sub': str(self.id)}
        # TODO
        return profile


class Client(db.Model, OAuth2ClientMixin):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id', ondelete='CASCADE')
    )
    user = db.relationship('User')
    allowed_response_types = db.Column(db.Text, default='code token')
    allowed_grant_types = db.Column(db.Text, default='')

    def check_response_type(self, response_type):
        response_types = response_type.split()
        allowed_types = self.allowed_response_types.split()
        return all([t in allowed_types for t in response_types])

    def check_grant_type(self, grant_type):
        return grant_type in self.allowed_grant_types.split()


class AuthorizationCode(db.Model, OAuth2AuthorizationCodeMixin):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id', ondelete='CASCADE')
    )
    user = db.relationship('User')


class Token(db.Model, OAuth2TokenMixin):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey('user.id', ondelete='CASCADE')
    )
    user = db.relationship('User')

    def is_refresh_token_expired(self):
        expired_at = self.issued_at + self.expires_in * 2
        return expired_at < time.time()


class CodeGrantMixin(object):
    def create_authorization_code(self, client, grant_user, request):
        code = generate_token(48)
        nonce = request.data.get('nonce')
        item = AuthorizationCode(
            code=code,
            client_id=client.client_id,
            redirect_uri=request.redirect_uri,
            scope=request.scope,
            nonce=nonce,
            user_id=grant_user.get_user_id(),
        )
        db.session.add(item)
        db.session.commit()
        return code

    def parse_authorization_code(self, code, client):
        item = AuthorizationCode.query.filter_by(
            code=code, client_id=client.client_id).first()
        if item and not item.is_expired():
            return item

    def delete_authorization_code(self, authorization_code):
        db.session.delete(authorization_code)
        db.session.commit()

    def authenticate_user(self, authorization_code):
        return User.query.get(authorization_code.user_id)


class AuthorizationCodeGrant(CodeGrantMixin, _AuthorizationCodeGrant):
    pass


class OpenIDCodeGrant(CodeGrantMixin, _OpenIDCodeGrant):
    pass


class PasswordGrant(_PasswordGrant):
    def authenticate_user(self, username, password):
        user = User.query.filter_by(username=username).first()
        if user.check_password(password):
            return user


class RefreshTokenGrant(_RefreshTokenGrant):
    def authenticate_refresh_token(self, refresh_token):
        item = Token.query.filter_by(refresh_token=refresh_token).first()
        if item and not item.is_refresh_token_expired():
            return item

    def authenticate_user(self, credential):
        return User.query.get(credential.user_id)


def create_authorization_server(app):
    query_client = create_query_client_func(db.session, Client)
    save_token = create_save_token_func(db.session, Token)

    def exists_nonce(nonce, req):
        exists = AuthorizationCode.query.filter_by(
            client_id=req.client_id, nonce=nonce
        ).first()
        return bool(exists)

    server = AuthorizationServer(
        app,
        query_client=query_client,
        save_token=save_token,
    )
    server.register_hook('exists_nonce', exists_nonce)

    @app.route('/oauth/authorize', methods=['GET', 'POST'])
    def authorize():
        if request.method == 'GET':
            try:
                server.validate_authorization_request()
                return 'ok'
            except OAuth2Error as error:
                return error.error
        user_id = request.form.get('user_id')
        if user_id:
            grant_user = User.query.get(int(user_id))
        else:
            grant_user = None
        return server.create_authorization_response(grant_user=grant_user)

    @app.route('/oauth/token', methods=['GET', 'POST'])
    def issue_token():
        return server.create_token_response()

    @app.route('/oauth/revoke', methods=['POST'])
    def revoke_token():
        return server.create_endpoint_response('revocation')

    return server


def create_resource_server(app):
    require_oauth = ResourceProtector()
    BearerTokenValidator = create_bearer_token_validator(db.session, Token)
    require_oauth.register_token_validator('bearer', BearerTokenValidator())

    @app.route('/user')
    @require_oauth('profile')
    def user_profile():
        user = current_token.user
        return jsonify(id=user.id, username=user.username)

    @app.route('/user/email')
    @require_oauth('email')
    def user_email():
        user = current_token.user
        return jsonify(email=user.username + '@example.com')

    @app.route('/info')
    @require_oauth()
    def public_info():
        return jsonify(status='ok')


def create_flask_app():
    app = Flask(__name__)
    app.debug = True
    app.testing = True
    app.secret_key = 'testing'
    app.config.update({
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        'OAUTH2_ERROR_URIS': [
            ('invalid_client', 'https://a.b/e#invalid_client')
        ]
    })
    return app


class TestCase(unittest.TestCase):
    def setUp(self):
        app = create_flask_app()

        self._ctx = app.app_context()
        self._ctx.push()

        db.init_app(app)
        db.create_all()

        self.app = app
        self.client = app.test_client()

    def tearDown(self):
        db.drop_all()
        self._ctx.pop()

    def create_basic_header(self, username, password):
        text = '{}:{}'.format(username, password)
        auth = to_unicode(base64.b64encode(to_bytes(text)))
        return {'Authorization': 'Basic ' + auth}
