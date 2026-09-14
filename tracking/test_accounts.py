import uuid

from django.core.exceptions import ValidationError
from django.test import TestCase

from .forms import AccountForm, LoginForm
from .models import User
from .views import business_data, save_account


class SimpleAccountTests(TestCase):
    def setUp(self):
        self.admin=User.objects.create_user(username='administrator',password='admin-test',role='admin')
        self.client.force_login(self.admin)

    def payload(self, password='123456', **extra):
        return {'username':'operator','role':'purchasing','new_password':password,
                'token':str(uuid.uuid4()),**extra}

    def test_create_with_six_characters_and_optional_profile_then_login(self):
        response=self.client.post('/accounts/new/',self.payload())
        self.assertEqual(response.status_code,302)
        user=User.objects.get(username='operator')
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password('123456'))
        self.assertEqual(user.email,'')
        self.assertEqual(user.first_name,'')
        self.assertTrue(LoginForm(data={'username':'operator','password':'123456'}).is_valid())

    def test_short_or_missing_password_cannot_create_account(self):
        for password in ['12345','']:
            with self.subTest(password=password):
                form=AccountForm(data=self.payload(password))
                self.assertFalse(form.is_valid())
                self.assertIn('new_password',form.errors)
                self.assertEqual(self.client.post('/accounts/new/',self.payload(password)).status_code,400)
        self.assertFalse(User.objects.filter(username='operator').exists())

    def test_service_enforces_minimum_even_without_form(self):
        with self.assertRaises(ValidationError):
            save_account(self.admin,{'username':'operator','role':'purchasing','is_active':True},'12345')
        self.assertFalse(User.objects.filter(username='operator').exists())

    def test_blank_edit_keeps_password_and_active_control(self):
        user=User.objects.create_user(username='operator',password='old-secret',role='purchasing')
        previous=user.password
        form=AccountForm(data=self.payload(''),instance=user)
        self.assertIn('is_active',form.fields)
        self.assertTrue(form.is_valid(),form.errors)
        save_account(self.admin,business_data(form),form.cleaned_data['new_password'],pk=user.pk)
        user.refresh_from_db()
        self.assertEqual(user.password,previous)
        self.assertFalse(user.is_active)

    def test_edit_accepts_six_character_password(self):
        user=User.objects.create_user(username='operator',password='old-secret',role='purchasing')
        form=AccountForm(data=self.payload(is_active=True),instance=user)
        self.assertTrue(form.is_valid(),form.errors)
        save_account(self.admin,business_data(form),form.cleaned_data['new_password'],pk=user.pk)
        user.refresh_from_db()
        self.assertTrue(user.check_password('123456'))

    def test_creation_hides_active_control_and_defaults_active(self):
        form=AccountForm(data=self.payload(is_active=False))
        self.assertNotIn('is_active',form.fields)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertTrue(business_data(form)['is_active'])

    def test_only_super_admin_can_create_accounts(self):
        for role in ['purchasing','director']:
            actor=User.objects.create_user(username=role,role=role)
            self.client.force_login(actor)
            self.assertEqual(self.client.post('/accounts/new/',self.payload()).status_code,403)
        self.assertFalse(User.objects.filter(username='operator').exists())