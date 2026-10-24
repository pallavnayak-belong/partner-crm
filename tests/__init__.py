"""Runs before any test module is imported. Points every data and settings path at a throwaway folder, so a test that forgets to
isolate itself can never touch the real CRM (a test once wrote fake emails and an unsubscribe into live data)."""
import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="crm-test-sandbox-")
for _k, _v in {"PARTNER_CRM_DB": "sandbox.db", "PARTNER_CRM_MAIL_CFG": "mail.json", "PARTNER_CRM_GDRIVE_CFG": "gdrive.json", "PARTNER_CRM_BACKUP_CFG": "backup.json",
               "PARTNER_CRM_SLACK_CFG": "slack.json", "PARTNER_CRM_ATTR_CFG": "attr.json", "PARTNER_CRM_YOUFORM_CFG": "youform.json", "PARTNER_CRM_VAULT_KEY": "vault.json", "PARTNER_CRM_WATCH_STATE": "watch.json"}.items():
    os.environ[_k] = os.path.join(_SANDBOX, _v)        # forced, not setdefault: a shell variable must not point a test at real data
os.environ["PARTNER_CRM_TEST_SANDBOX"] = _SANDBOX
