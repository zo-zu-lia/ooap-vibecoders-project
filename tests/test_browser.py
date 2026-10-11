"""Real browser journeys against the local Docker stack, including OIDC and S3.

Run after `docker compose up -d` with Chromium installed by Playwright.
No browser session, API response, checksum, or storage upload is mocked.
"""
import json
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import expect, sync_playwright

from conftest import ORIGIN, ROOT

pytestmark = pytest.mark.integration


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        yield browser
        browser.close()


def login(page, role='owner'):
    page.goto(ORIGIN)
    page.get_by_role('link', name='Sign in with your company').click()
    page.locator('input[name="login"]').fill(f'{role}@fileflow.local')
    password = json.loads((ROOT / '.local/test-credentials.json').read_text())['password']
    page.locator('input[name="password"]').fill(password)
    page.locator('button[type="submit"]').click()
    expect(page.get_by_role('button', name='Sign out', exact=True)).to_be_visible(timeout=20000)


@pytest.fixture
def owner_page(browser):
    context = browser.new_context(viewport={'width': 1360, 'height': 950}, accept_downloads=True)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    login(page)
    yield page
    context.close()
    assert errors == [], f'Browser runtime errors: {errors}'


def upload(page, label, name, data):
    with page.expect_file_chooser() as chooser:
        page.get_by_role('button', name=label, exact=True).click()
    chooser.value.set_files({'name': name, 'mimeType': 'text/plain', 'buffer': data})
    transfer = page.locator('.transfer').filter(has=page.get_by_text(name, exact=True))
    expect(transfer.get_by_text('Ready · integrity verified and scan passed', exact=True)).to_be_visible(timeout=120000)


def download(page):
    with page.expect_download(timeout=20000) as event:
        page.get_by_role('button', name='Download', exact=True).click()
    result = event.value
    assert result.failure() is None
    return Path(result.path()).read_bytes()


def test_owner_multipart_history_download_and_trash(owner_page):
    page = owner_page
    folder_name = 'Browser documents ' + uuid4().hex[:8]
    page.get_by_role('button', name='New folder', exact=True).click()
    page.get_by_label('Folder name', exact=True).fill(folder_name)
    page.get_by_role('button', name='Create folder', exact=True).click()
    page.get_by_role('button', name=folder_name, exact=True).click()
    name = 'browser-' + uuid4().hex[:8] + '.txt'
    original = (b'Browser multipart verification.\n' * 300000)  # > 8 MiB; real two-part S3 PUT.
    upload(page, 'Upload files', name, original)
    page.screenshot(path=str(ROOT / '.local/browser-desktop.png'), full_page=True)
    page.get_by_role('button', name=name, exact=True).click()
    expect(page.get_by_role('heading', name=name, exact=True)).to_be_visible()
    assert download(page) == original
    second_name = 'new-version-' + uuid4().hex[:8] + '.txt'
    replacement = b'Updated document from the browser.\n'
    upload(page, 'Upload new version', second_name, replacement)
    page.get_by_role('button', name=name, exact=True).click()
    expect(page.locator('.version-meta').get_by_text('Version 2', exact=True)).to_be_visible()
    assert download(page) == replacement
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Restore version', exact=True).click()
    expect(page.locator('.version-meta').get_by_text('Version 3', exact=True)).to_be_visible()
    assert download(page) == original
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Move to trash', exact=True).click()
    expect(page.get_by_role('dialog')).not_to_be_visible()
    page.get_by_role('button', name='Trash', exact=True).click()
    row = page.get_by_role('row').filter(has=page.get_by_role('button', name=name, exact=True))
    row.get_by_role('button', name='Restore', exact=True).click()
    expect(row).to_have_count(0)


def test_keyboard_mobile_and_useful_error(owner_page):
    page = owner_page
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.keyboard.press('Control+Home')
    page.get_by_role('button', name='New folder', exact=True).focus()
    page.keyboard.press('Enter')
    expect(page.get_by_label('Folder name', exact=True)).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.get_by_role('dialog')).not_to_be_visible()
    folder_name = 'Duplicate check ' + uuid4().hex[:8]
    for attempt in range(2):
        page.get_by_role('button', name='New folder', exact=True).click()
        page.get_by_label('Folder name', exact=True).fill(folder_name)
        page.get_by_role('button', name='Create folder', exact=True).click()
        if attempt == 0:
            expect(page.get_by_role('dialog')).not_to_be_visible()
    expect(page.locator('.dialog-error')).to_be_visible()
    expect(page.locator('.dialog-error')).to_contain_text('Request ID:')
    expect(page.get_by_label('Folder name', exact=True)).to_have_value(folder_name)
    page.keyboard.press('Escape')
    with page.expect_file_chooser() as chooser:
        page.get_by_role('button', name='Upload files', exact=True).click()
    chooser.value.set_files({'name': 'empty.txt', 'mimeType': 'text/plain', 'buffer': b''})
    expect(page.locator('#notices')).to_contain_text('is empty')
    (ROOT / '.local').mkdir(exist_ok=True)
    page.screenshot(path=str(ROOT / '.local/browser-mobile.png'), full_page=True)


def test_viewer_controls_and_session_signout(browser):
    context = browser.new_context(viewport={'width': 1280, 'height': 850})
    try:
        page = context.new_page()
        login(page, 'viewer')
        expect(page.get_by_role('button', name='New folder', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='Upload files', exact=True)).to_have_count(0)
        expect(page.get_by_role('button', name='Workspace settings', exact=True)).to_have_count(0)
        page.get_by_role('button', name='Sign out', exact=True).click()
        expect(page.get_by_role('link', name='Sign in with your company')).to_be_visible()
        assert page.request.get(ORIGIN + '/api/v1/me').status == 401
    finally:
        context.close()


def test_admin_analytics_and_recovery_views(browser):
    context = browser.new_context(viewport={'width': 1360, 'height': 950})
    errors = []
    try:
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        login(page, 'security')
        page.get_by_role('button', name='Workspace settings', exact=True).click()
        expect(page.get_by_role('heading', name='Workspace policy', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='Legal hold', exact=True)).to_be_visible()
        page.get_by_role('button', name='Analytics', exact=True).click()
        expect(page.locator('.metric-card')).to_have_count(12, timeout=20000)
        expect(page.get_by_role('heading', name='From upload to access', exact=True)).to_be_visible()
        page.get_by_role('button', name='Failed jobs', exact=True).click()
        expect(page.get_by_role('heading', name='Failed jobs', exact=True)).to_be_visible()
        page.get_by_role('button', name='Audit log', exact=True).click()
        expect(page.get_by_role('heading', name='Audit log', exact=True)).to_be_visible()
        expect(page.locator('.audit-table tbody tr').first).to_be_visible()
        assert errors == []
    finally:
        context.close()


def test_personal_access_discovery_share_recipient_and_revocation(owner_page, browser):
    page = owner_page
    name = 'browser-sharing-' + uuid4().hex[:8] + '.txt'
    content = b'Only the addressed company colleague can open the share link.\n'
    upload(page, 'Upload files', name, content)
    page.get_by_role('button', name=name, exact=True).click()
    file_id = page.locator('.file-identifier .mono').text_content()
    page.get_by_role('button', name='Manage access', exact=True).click()
    finance_id = '00000000-0000-4000-8000-000000000005'
    page.get_by_label('Colleague', exact=True).select_option(finance_id)
    page.get_by_role('button', name='Save access', exact=True).click()
    expect(page.locator('.access-row').filter(has_text='Can view')).to_be_visible()
    page.get_by_label('Link recipient', exact=True).select_option(finance_id)
    page.get_by_role('button', name='Create link', exact=True).click()
    link_field = page.get_by_label('New share link', exact=True)
    expect(link_field).to_be_visible()
    link = link_field.input_value()
    assert '#share=' in link
    recipient_context = browser.new_context(accept_downloads=True)
    try:
        recipient = recipient_context.new_page()
        recipient.goto(link)
        expect(recipient.get_by_role('link', name='Sign in with your company')).to_be_visible()
        assert '#share=' not in recipient.url, 'Share secret must leave the address bar before SSO'
        login(recipient, 'finance')
        expect(recipient.get_by_role('heading', name=name, exact=True)).to_be_visible()
        with recipient.expect_download() as event:
            recipient.get_by_role('button', name='Download file', exact=True).click()
        assert Path(event.value.path()).read_bytes() == content
        page.get_by_role('button', name='Revoke', exact=True).click()
        recipient.reload()
        expect(recipient.get_by_role('heading', name='Could not open this share link', exact=True)).to_be_visible()
        recipient.goto(ORIGIN + '/#file=' + file_id)
        expect(recipient.get_by_role('heading', name=name, exact=True)).to_be_visible()
        recipient.get_by_role('button', name='Done', exact=True).click()
        recipient.get_by_role('button', name='Shared with me', exact=True).click()
        expect(recipient.get_by_role('button', name=name, exact=True)).to_be_visible()
        recipient.get_by_role('button', name=name, exact=True).click()
        expect(recipient.get_by_role('button', name='Manage access', exact=True)).to_have_count(0)
        expect(recipient.get_by_role('button', name='Upload new version', exact=True)).to_have_count(0)
        assert download(recipient) == content
    finally:
        recipient_context.close()
