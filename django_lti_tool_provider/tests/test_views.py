import ddt
from django.contrib.auth import login, authenticate
from importlib import import_module
from django_lti_tool_provider import AbstractApplicationHookManager
from mock import patch, Mock

from oauth2 import Request, Consumer, SignatureMethod_HMAC_SHA1

from django.contrib.auth.models import User
from django.test.utils import override_settings
from django.test import Client, TestCase, RequestFactory
from django.conf import settings

from django_lti_tool_provider.models import LtiUserData
from django_lti_tool_provider.views import LTIView


@override_settings(
    LTI_CLIENT_KEY='qertyuiop1234567890!@#$%^&*()_+[];',
    LTI_CLIENT_SECRET='1234567890!@#$%^&*()_+[];./,;qwertyuiop'
)
class LtiRequestsTestBase(TestCase):
    _data = {
        "lis_result_sourcedid": "lis_result_sourcedid",
        "context_id": "LTIX/LTI-101/now",
        "user_id": "1234567890",
        "roles": ["Student"],
        "lis_outcome_service_url": "lis_outcome_service_url",
        "resource_link_id": "resource_link_id",
        "lti_version": "LTI-1p0",
        'lis_person_sourcedid': 'username',
        'lis_person_contact_email_primary': 'username@email.com'
    }

    _url_base = 'http://testserver'

    DEFAULT_REDIRECT = '/home'

    def setUp(self):
        self.client = Client()
        self.hook_manager = Mock(spec=AbstractApplicationHookManager)
        self.hook_manager.vary_by_key = Mock(return_value=None)
        self.hook_manager.optional_lti_parameters = Mock(return_value={})
        LTIView.register_authentication_manager(self.hook_manager)

    @property
    def consumer(self):
        return Consumer(settings.LTI_CLIENT_KEY, settings.LTI_CLIENT_SECRET)

    def _get_signed_oauth_request(self, path, method, data=None):
        data = data if data is not None else self._data
        url = self._url_base + path
        method = method if method else 'GET'
        req = Request.from_consumer_and_token(self.consumer, {}, method, url, data)
        req.sign_request(SignatureMethod_HMAC_SHA1(), self.consumer, None)
        return req

    def get_correct_lti_payload(self, path='/lti/', method='POST', data=None):
        req = self._get_signed_oauth_request(path, method, data)
        return req.to_postdata()

    def get_incorrect_lti_payload(self, path='/lti/', method='POST', data=None):
        req = self._get_signed_oauth_request(path, method, data)
        req['oauth_signature'] += '_broken'
        return req.to_postdata()

    def send_lti_request(self, payload, client=None):
        client = client or self.client
        return client.post('/lti/', payload, content_type='application/x-www-form-urlencoded')

    def _authenticate(self, username='test'):
        self.client = Client()
        user = User.objects.get(username=username)
        logged_in = self.client.login(username=username, password='test')
        self.assertTrue(logged_in)
        return user

    def _logout(self):
        self.client.logout()

    def _verify_redirected_to(self, response, expected_url):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, expected_url)

    def _verify_session_lti_contents(self, session, expected):
        self.assertIn('lti_parameters', session)
        self._verify_lti_data(session['lti_parameters'], expected)

    def _verify_lti_data(self, actual, expected):
        for key, value in expected.items():
            self.assertEqual(value, actual[key])

    def _verify_lti_created(self, user, expected_lti_data, custom_key=None):
        key = custom_key if custom_key else ''
        lti_data = LtiUserData.objects.get(user=user, custom_key=key)
        self.assertIsNotNone(lti_data)
        self.assertEqual(lti_data.custom_key, key)
        for key, value in expected_lti_data.items():
            self.assertEqual(value, lti_data.edx_lti_parameters[key])


class AnonymousLtiRequestTests(LtiRequestsTestBase):
    def setUp(self):
        super(AnonymousLtiRequestTests, self).setUp()
        self.hook_manager.anonymous_redirect_to = Mock(return_value=self.DEFAULT_REDIRECT)

    def test_given_incorrect_payload_throws_bad_request(self):
        response = self.send_lti_request(self.get_incorrect_lti_payload())
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid LTI Request", response.content)

    def test_given_correct_requests_sets_session_variable(self):
        response = self.send_lti_request(self.get_correct_lti_payload())

        self._verify_redirected_to(response, self._url_base + self.DEFAULT_REDIRECT)

        self._verify_session_lti_contents(self.client.session, self._data)


@ddt.ddt
@patch('django_lti_tool_provider.views.Signals.LTI.received.send')
class AuthenticatedLtiRequestTests(LtiRequestsTestBase):
    def _authentication_hook(self, request, user_id=None, username=None, email=None, **kwargs):
        user = User.objects.create_user(username or user_id, password='1234', email=email)
        user.save()
        user = authenticate(username=user.username, password='1234')
        login(request, user)
        return user

    def setUp(self):
        super(AuthenticatedLtiRequestTests, self).setUp()
        self.hook_manager.authenticated_redirect_to = Mock(return_value=self.DEFAULT_REDIRECT)
        self.hook_manager.authentication_hook = self._authentication_hook

    def _verify_lti_updated_signal_is_sent(self, patched_send_lti_received, expected_user):
        expected_lti_data = LtiUserData.objects.get(user=expected_user)
        patched_send_lti_received.assert_called_once_with(LTIView, user=expected_user, lti_data=expected_lti_data)

    def test_no_session_given_incorrect_payload_throws_bad_request(self, _):
        response = self.send_lti_request(self.get_incorrect_lti_payload())
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid LTI Request", response.content)

    def test_no_session_correct_payload_processes_lti_request(self, patched_send_lti_received):
        # Precondition check
        self.assertFalse(LtiUserData.objects.all())

        response = self.send_lti_request(self.get_correct_lti_payload())

        # Should have been created.
        user = User.objects.all()[0]
        self._verify_lti_created(user, self._data)
        self._verify_redirected_to(response, self._url_base + self.DEFAULT_REDIRECT)
        self._verify_lti_updated_signal_is_sent(patched_send_lti_received, user)

    def test_given_session_and_lti_uses_lti(self, patched_send_lti_received):
        # Precondition check
        self.assertFalse(LtiUserData.objects.all())

        session = self.client.session
        session['lti_parameters'] = {}
        session.save()

        response = self.send_lti_request(self.get_correct_lti_payload())

        # Should have been created.
        user = User.objects.all()[0]
        self._verify_lti_created(user, self._data)
        self._verify_redirected_to(response, self._url_base + self.DEFAULT_REDIRECT)
        self._verify_lti_updated_signal_is_sent(patched_send_lti_received, user)

    def test_force_login_change(self, patched_send_lti_received):
        self.assertFalse(User.objects.exclude(id=1))
        payload = self.get_correct_lti_payload()
        request = self.send_lti_request(payload, client=RequestFactory())
        engine = import_module(settings.SESSION_ENGINE)
        request.session = engine.SessionStore()
        request.user = None
        user = self._authentication_hook(request, username='goober')
        request.session.save()
        self.assertEqual(request.user, user)
        LTIView.as_view()(request)
        # New user creation not actually available during tests.
        self.assertTrue(request.user)
        new_user = User.objects.exclude(username='goober')[0]
        self.assertEqual(request.user, new_user)

        # Verify a new user is not created with the same data if re-visiting.
        request = self.send_lti_request(payload, client=RequestFactory())
        request.session = engine.SessionStore()
        request.user = None
        user = authenticate(username=new_user.username, password='1234')
        self.assertTrue(user)
        login(request, user)
        LTIView.as_view()(request)
        self.assertEqual(request.user, user)
        self.assertEqual(user, new_user)

        self.assertEqual(LtiUserData.objects.all().count(), 1)

@ddt.ddt
class AuthenticationManagerIntegrationTests(LtiRequestsTestBase):
    TEST_URLS = ("/some_url", False), ("/some_other_url", False), ("http://qwe.asd.zxc.com", True)

    def setUp(self):
        super(AuthenticationManagerIntegrationTests, self).setUp()

    def tearDown(self):
        LTIView.authentication_manager = None
        self._logout()

    def _authenticate_user(self, request, user_id=None, username=None, email=None, **kwargs):
        if not username:
            username = "test_username"
        password = "test_password"

        user = User.objects.create_user(username=username, email=email, password=password)
        authenticated = authenticate(username=username, password=password)
        login(request, authenticated)

        self.addCleanup(lambda: user.delete())

    def test_authentication_hook_executed_if_not_authenticated(self):
        payload = self.get_correct_lti_payload()
        self.send_lti_request(payload)
        args, user_data = self.hook_manager.authentication_hook.call_args
        request = args[0]
        self.assertEqual(request.body, payload)
        self.assertFalse(request.user.is_authenticated())
        expected_user_data = {
            'username': self._data['lis_person_sourcedid'],
            'email': self._data['lis_person_contact_email_primary'],
            'user_id': self._data['user_id'],
            'extra_params': {}
        }
        self.assertEqual(user_data, expected_user_data)

    def test_authentication_hook_passes_optional_lti_data(self):
        payload = self.get_correct_lti_payload()
        self.hook_manager.optional_lti_parameters.return_value = {'resource_link_id': 'link_id', 'roles': 'roles'}
        self.send_lti_request(payload)
        args, user_data = self.hook_manager.authentication_hook.call_args
        request = args[0]
        self.assertEqual(request.body, payload)
        self.assertFalse(request.user.is_authenticated())
        expected_user_data = {
            'username': self._data['lis_person_sourcedid'],
            'email': self._data['lis_person_contact_email_primary'],
            'user_id': self._data['user_id'],
            'extra_params': {
                'roles': ['Student'],
                'link_id': 'resource_link_id',
            }
        }
        self.assertEqual(user_data, expected_user_data)

    @ddt.data(*TEST_URLS)
    @ddt.unpack
    def test_anonymous_lti_is_processed_if_hook_does_not_authenticate_user(self, url, absolute):
        self.hook_manager.anonymous_redirect_to.return_value = url
        response = self.send_lti_request(self.get_correct_lti_payload())

        expected_url = url if absolute else "{base}{url}".format(base=self._url_base, url=url)
        self._verify_redirected_to(response, expected_url)

        self._verify_session_lti_contents(self.client.session, self._data)

        # verifying correct parameters were passed to auth manager hook
        request, lti_data = self.hook_manager.anonymous_redirect_to.call_args[0]
        self._verify_session_lti_contents(request.session, self._data)
        self._verify_lti_data(lti_data, self._data)

    @ddt.data(*TEST_URLS)
    @ddt.unpack
    def test_authenticated_lti_is_processed_if_hook_authenticates_user(self, url, absolute):
        self.hook_manager.authentication_hook.side_effect = self._authenticate_user
        self.hook_manager.authenticated_redirect_to.return_value = url
        response = self.send_lti_request(self.get_correct_lti_payload())

        expected_url = url if absolute else "{base}{url}".format(base=self._url_base, url=url)
        self._verify_redirected_to(response, expected_url)

        # verifying correct parameters were passed to auth manager hook
        request, lti_data = self.hook_manager.authenticated_redirect_to.call_args[0]
        user = request.user
        self._verify_lti_created(user, self._data)
        self._verify_lti_data(lti_data, self._data)

    @ddt.data('custom', 'very custom', 'extremely custom')
    def test_authenticated_lti_saves_custom_key_if_specified(self, key):
        self.hook_manager.vary_by_key.return_value = key
        self.hook_manager.authentication_hook.side_effect = self._authenticate_user

        self.send_lti_request(self.get_correct_lti_payload())

        request, lti_data = self.hook_manager.authenticated_redirect_to.call_args[0]
        user = request.user
        self._verify_lti_created(user, self._data, key)
