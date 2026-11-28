"""Uninstalled design aid for an optional pinned catalogue unit in native CD.

NOT a deployer or a drop-in patch. Production integration must independently
obtain these observations from systemd, root-owned files and the kernel cgroup
tree, under the existing controller deploy lock. Test on Linux before adoption.
"""
import hashlib
import hmac

UNIT = '3dprinta-model-catalog.service'
PORTAL = '3dprinta-portal.service'


def check_configuration(portal_consists_of, props, unit_cat, launcher, policy):
    """Validate trusted observations against root-owned, pre-reviewed pins.

    Return False for the exact legacy state; True for the exact catalogue
    extension. Any partial, unknown or unrelated state is refused.
    """
    loaded = props.get('LoadState')
    members = portal_consists_of.split()
    if loaded == 'not-found' and not members:
        if (props.get('ActiveState') != 'inactive' or props.get('MainPID') != '0'
                or props.get('ControlGroup') != '' or props.get('InvocationID') != ''):
            raise ValueError('Absent catalogue unit still has state')
        return False
    if loaded != 'loaded' or members != [UNIT]:
        raise ValueError('Unexpected stop-propagation graph')
    required = {'NeedDaemonReload':'no', 'PartOf':PORTAL, 'Requisite':PORTAL,
                'KillMode':'control-group', 'DynamicUser':'yes',
                'User':'wbozon-portal', 'Restart':'no',
                'ExecStop':'', 'ExecStopPost':'', 'PropagatesStopTo':'',
                'OnSuccess':'', 'OnFailure':''}
    if any(props.get(name) != value for name, value in required.items()):
        raise ValueError('Unexpected catalogue unit properties')
    if PORTAL not in props.get('After', '').split():
        raise ValueError('Missing stop ordering')
    for actual, key in ((unit_cat, 'unit_sha256'), (launcher, 'launcher_sha256')):
        expected = policy.get(key)
        if not isinstance(actual, bytes) or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError('Missing reviewed pin')
        if not hmac.compare_digest(hashlib.sha256(actual).hexdigest(), expected):
            raise ValueError('Unreviewed unit or launcher')
    return True


def require_stopped(props, process_ids):
    """Refuse switch/prune unless systemd and fixed kernel cgroup agree."""
    if (props.get('ActiveState') not in ('inactive', 'failed') or props.get('MainPID') != '0'
            or not isinstance(process_ids, (tuple, list, set)) or process_ids):
        raise ValueError('Catalogue worker has not stopped')
