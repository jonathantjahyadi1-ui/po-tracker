        self.assertFalse(user.is_superuser or user.can_adjust or user.can_reopen)
        entry = Audit.objects.get(action=BOOTSTRAP_ACTION)
        self.assertEqual(entry.actor_id, user.pk)
        self.assertNotIn('password', entry.after)
        for model in [Master, Movement, Order, Receipt]:
            self.assertFalse(model.objects.exists())
        self.client.force_login(user)
        self.assertEqual(self.client.get('/accounts/').status_code, 200)
        self.assertEqual(self.client.get('/accounts/new/').status_code, 200)
        self.assertEqual(self.client.get('/receipts/new/').status_code, 403)

    def test_repeated_setup_preserves_all_account_changes(self):
        self.bootstrap()
        self.bootstrap()
        user = User.objects.get()
        user.username = 'Jonathan-renamed'
        user.first_name = 'Updated'
        user.email = 'updated@example.test'
        user.role = User.Role.MANAGEMENT
        user.is_active = False
        user.set_password('Changed-password-for-test-1234')
        user.save()
        changed_password = user.password
        self.bootstrap()
        user.refresh_from_db()
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Audit.objects.filter(action=BOOTSTRAP_ACTION).count(), 1)
        self.assertEqual(user.username, 'Jonathan-renamed')
        self.assertEqual(user.first_name, 'Updated')
        self.assertEqual(user.email, 'updated@example.test')
        self.assertEqual(user.role, User.Role.MANAGEMENT)
        self.assertFalse(user.is_active)
        self.assertEqual(user.password, changed_password)

    def test_username_collision_does_not_elevate_existing_account(self):
        user = User.objects.create_user(username=INITIAL_USERNAME.lower())
        with self.assertRaises(CommandError):
            self.bootstrap()
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.MANAGEMENT)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(Audit.objects.exists())

    def test_email_collision_does_not_replace_existing_account(self):
        user = User.objects.create_user(username='other', email=INITIAL_EMAIL.upper())
        with self.assertRaises(CommandError):
            self.bootstrap()
        user.refresh_from_db()
        self.assertEqual(user.username, 'other')
        self.assertEqual(user.role, User.Role.MANAGEMENT)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(Audit.objects.exists())

    def test_password_login_is_required(self):
        test_password = 'Test-only-password-1234'
        with patch(
            'tracking.management.commands.bootstrap_admin.INITIAL_PASSWORD_HASH',
            make_password(test_password),
        ):
            self.bootstrap()
        response = self.client.post('/login/', {'username': INITIAL_USERNAME, 'password': 'incorrect'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        response = self.client.post('/login/', {'username': INITIAL_USERNAME, 'password': test_password})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), User.objects.get().pk)

    def test_demo_entry_points_are_removed_even_in_debug_mode(self):
        self.assertNotIn('seed_demo', get_commands())
        with self.assertRaises(NoReverseMatch):
            reverse('demo_login')
        for debug in [True, False]:
            with override_settings(DEBUG=debug):
                self.assertNotContains(self.client.get('/login/'), 'demo', html=False)
                self.client.post('/demo-login/', {'role': 'admin'})
                self.assertNotIn('_auth_user_id', self.client.session)
        self.bootstrap()
        self.client.force_login(User.objects.get())
        self.assertEqual(self.client.post('/demo-login/', {'role': 'admin'}).status_code, 404)
        self.assertNotContains(self.client.get('/'), 'DATA CONTOH')

    def test_audit_failure_rolls_back_account_creation(self):
        with patch('tracking.management.commands.bootstrap_admin.Audit.objects.create', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.bootstrap()
        self.assertFalse(User.objects.exists())
        self.assertFalse(Audit.objects.exists())


@skipUnless(connection.vendor == 'postgresql', 'Concurrency membutuhkan PostgreSQL asli.')
class BootstrapConcurrencyTests(TransactionTestCase):
    def test_concurrent_deploys_create_one_account(self):
        barrier = threading.Barrier(2)

        def run(_):
            try:
                barrier.wait(timeout=10)
                call_command('bootstrap_admin', stdout=io.StringIO())
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, [1, 2]))
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Audit.objects.filter(action=BOOTSTRAP_ACTION).count(), 1)