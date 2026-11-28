import hashlib
import unittest
import catalog_cd_guard as guard


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.cat, self.launch = b'fixed unit', b'fixed launcher'
        self.policy = {'unit_sha256':hashlib.sha256(self.cat).hexdigest(),
                       'launcher_sha256':hashlib.sha256(self.launch).hexdigest()}
        self.props = {'LoadState':'loaded','NeedDaemonReload':'no','PartOf':guard.PORTAL,
                      'Requisite':guard.PORTAL,'After':'basic.target '+guard.PORTAL,
                      'KillMode':'control-group','DynamicUser':'yes','User':'wbozon-portal',
                      'Restart':'no','ExecStop':'','ExecStopPost':'','PropagatesStopTo':'',
                      'OnSuccess':'','OnFailure':''}

    def check(self, members=None, props=None, cat=None, launch=None):
        return guard.check_configuration(guard.UNIT if members is None else members,
            self.props if props is None else props, self.cat if cat is None else cat,
            self.launch if launch is None else launch, self.policy)

    def test_legacy_and_exact_unit(self):
        self.assertFalse(guard.check_configuration('', {'LoadState':'not-found','ActiveState':'inactive',
            'MainPID':'0','ControlGroup':'','InvocationID':''}, b'', b'', {}))
        self.assertTrue(self.check())

    def test_absent_unit_with_residual_state_is_rejected(self):
        base = {'LoadState':'not-found','ActiveState':'inactive','MainPID':'0','ControlGroup':'','InvocationID':''}
        for field, value in (('ActiveState','active'), ('MainPID','123'), ('ControlGroup','/some/group'), ('InvocationID','123')):
            with self.assertRaises(ValueError): guard.check_configuration('', {**base,field:value}, b'', b'', {})

    def test_forbids_foreign_units_changed_pins_and_properties(self):
        for members in ('other.service', guard.UNIT + ' other.service', ''):
            with self.assertRaises(ValueError): self.check(members=members)
        for field, value in (('PartOf',''), ('Requisite',''), ('After','basic.target'),
                             ('KillMode','process'), ('ExecStop','arbitrary'),
                             ('OnFailure','other.service'), ('NeedDaemonReload','yes')):
            with self.assertRaises(ValueError): self.check(props={**self.props, field:value})
        with self.assertRaises(ValueError): self.check(cat=b'altered')
        with self.assertRaises(ValueError): self.check(launch=b'altered')

    def test_quiescence_requires_both_systemd_and_empty_cgroup(self):
        guard.require_stopped({'ActiveState':'inactive','MainPID':'0'}, [])
        for props, pids in (({'ActiveState':'active','MainPID':'0'}, []),
                            ({'ActiveState':'inactive','MainPID':'15'}, []),
                            ({'ActiveState':'inactive','MainPID':'0'}, [15]), ({}, [])):
            with self.assertRaises(ValueError): guard.require_stopped(props,pids)


if __name__ == '__main__': unittest.main()
