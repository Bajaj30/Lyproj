"""
Browser checks (Playwright, headless Chromium) of the doctor, administrator,
patient and pharmacy screens, against a live server that serves the built UI.

    python tests/run.py browser

Needs `pip install playwright && playwright install chromium`, and the UI built
for same-origin serving:  cd web_app && VITE_API_URL= npm run build
The patient and the public verify page run at phone width (390 px).
"""

import os
import re
import sys
import time

from PIL import Image
from playwright.sync_api import expect, sync_playwright

BASE = os.environ["E2E_BASE"]
LOG = os.environ["E2E_LOG"]
TMP = os.environ["E2E_TMP"]
ADMIN = (os.environ["E2E_ADMIN_EMAIL"], os.environ["E2E_ADMIN_PASSWORD"])
H = {"X-Requested-With": "PrescriptAI"}
PW = "Correct-Horse-9-Battery"
tag = str(int(time.time()))[-6:]
results = []
console = {}


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{str(extra)[:300]}]" if extra != "" and not cond else ""))


Image.new("RGB", (600, 800), "white").save(f"{TMP}/p1.png")
Image.new("RGB", (620, 800), "ivory").save(f"{TMP}/p2.png")


def new_ctx(browser, name, width=1280):
    ctx = browser.new_context(viewport={"width": width, "height": 900}, base_url=BASE)
    page = ctx.new_page()
    console[name] = []
    page.on("console", lambda m: m.type == "error" and console[name].append(m.text))
    return ctx, page


def no_hscroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


with sync_playwright() as p:
    browser = p.chromium.launch()

    # ── doctor registers; cannot do anything until approved ───────────────
    dctx, d = new_ctx(browser, "doctor")
    r = dctx.request.post("/api/auth/register", headers=H, data={
        "role": "doctor", "full_name": "Dr. Asha Rao", "email": f"asha{tag}@example.com", "password": PW,
        "profile": {"speciality": "General Medicine", "clinic_address": "12 MG Road, Pune", "registration_no": "MMC-2011-0457"}})
    check("doctor registered", r.ok)
    d.goto("/upload")
    expect(d.get_by_text("Your account is being verified")).to_be_visible()
    check("unapproved doctor sees the waiting screen, with the details being checked",
          d.get_by_text("MMC-2011-0457").is_visible() and d.get_by_label("Patient name").count() == 0)
    d.screenshot(path=f"{TMP}/shot_waiting.png", full_page=True)

    # ── administrator approves, in the UI ─────────────────────────────────
    actx, a = new_ctx(browser, "admin")
    a.goto("/login")
    a.get_by_label("Email, phone or username").fill(ADMIN[0])
    a.get_by_label("Password", exact=True).fill(ADMIN[1])
    a.get_by_role("button", name="Sign in").click()
    a.wait_for_url(re.compile(r"/admin$"))
    expect(a.get_by_text("Dr. Asha Rao")).to_be_visible()
    check("admin lands on the review queue and sees the registration number", a.get_by_text("MMC-2011-0457").is_visible())
    check("admin is told to turn on two-step verification", a.get_by_text("no two-step verification").is_visible())
    a.screenshot(path=f"{TMP}/shot_admin.png", full_page=True)
    a.get_by_role("button", name="Reject", exact=True).click()
    expect(a.get_by_text("Say why the account is being rejected")).to_be_visible()
    check("rejecting without a reason is refused, inline", True)
    a.get_by_placeholder(re.compile("Registration confirmed")).fill("Registration confirmed on the state council register")
    a.get_by_role("button", name="Approve", exact=True).click()
    expect(a.get_by_text("No accounts are waiting for review.")).to_be_visible()
    check("approved: the queue is empty", True)
    a.get_by_role("tab", name=re.compile("Approved")).click()
    expect(a.get_by_role("button", name="Withdraw approval")).to_be_visible()
    check("approved accounts can have approval withdrawn", True)

    d.get_by_role("button", name="Check again").click()
    expect(d.get_by_label("Patient name")).to_be_visible()
    check("doctor: 'Check again' opens the scan page once approved", True)

    # ── doctor: new consultation, two pages in one upload ─────────────────
    expect(d.get_by_text("Demo reader.")).to_be_visible()
    phone = f"+9198{tag}11"
    d.get_by_label("Patient name").fill("Ms. Prathna")
    d.get_by_label("Patient phone").fill(phone)
    check("clinic prefilled from profile", d.get_by_label("Clinic").input_value() == "12 MG Road, Pune")
    d.set_input_files("#page-input", [f"{TMP}/p1.png", f"{TMP}/p2.png"])
    expect(d.get_by_text("Page 2", exact=True)).to_be_visible()
    d.get_by_role("button", name=re.compile("Read 2 pages")).click()
    d.wait_for_url(re.compile(r"/consultations/"), timeout=60000)
    expect(d.get_by_role("heading", name="Ms. Prathna")).to_be_visible()
    check("two pages uploaded in one request land on the consultation",
          d.get_by_role("button", name="Page 2", exact=True).count() == 1)
    check("draft badge", d.get_by_text("Draft · not sent").is_visible())
    expect(d.get_by_text(re.compile("Page 2 reads the same as page 1")).first).to_be_visible()
    check("identical readings are flagged, in the banner and in the finalise panel",
          d.get_by_text(re.compile("Page 2 reads the same as page 1")).count() == 2)
    check("finalise is blocked until the duplicate is dealt with",
          d.get_by_role("button", name="Finalise and send to patient").is_disabled())
    d.get_by_role("button", name="Delete page 2").first.click()
    expect(d.get_by_text(re.compile("Page 2 reads the same as page 1"))).to_have_count(0)
    check("deleting the duplicate page clears the warning", d.get_by_role("button", name="Page 2", exact=True).count() == 0)

    card = d.locator("div.rounded-xl", has=d.get_by_text("Could not read", exact=True)).first
    card.get_by_role("button", name="Correct this").click()
    d.get_by_label("Correct value").fill("Zofer 4mg")
    d.get_by_role("button", name="Save correction").click()
    expect(d.get_by_text("Zofer 4mg", exact=True)).to_be_visible()
    check("correction re-resolves the illegible line", d.get_by_text("Ondansetron").first.is_visible())

    for name in ("Cofarin 1mg", "ALFAM 400MG"):
        d.get_by_role("button", name="Add a medicine the reader missed").click()
        d.get_by_label("Brand or generic name").fill(name)
        d.get_by_role("button", name="Add medicine", exact=True).click()
        expect(d.get_by_text(name, exact=True)).to_be_visible()
    check("added medicines show 'Entered by you'", d.get_by_text("Entered by you").count() >= 2)
    expect(d.get_by_text(re.compile(r"Overall risk: severe"))).to_be_visible()
    check("severe interaction across the consultation", True)

    d.get_by_role("button", name="Finalise and send to patient").click()
    expect(d.get_by_text(re.compile(r"Version 1 finalised"))).to_be_visible()
    vid = d.locator("a[href^='/check/PRX-']").first.inner_text()
    check("finalised: version listed with its verification ID", vid.startswith("PRX-"), vid)
    check("finalise button now says current", d.get_by_role("button", name="Version 1 is current").is_disabled())

    # the same photo again is refused, with the reason on screen
    d.locator("aside input[type=file]").set_input_files(f"{TMP}/p1.png")
    expect(d.get_by_text("This image is already page 1 of this prescription.")).to_be_visible()
    check("adding the same photo again is refused on screen", True)
    d.screenshot(path=f"{TMP}/shot_doctor.png", full_page=True)

    d.goto("/history")
    expect(d.get_by_text("finalised v1")).to_be_visible()
    check("history lists the consultation", True)

    # ── patient (phone width) ─────────────────────────────────────────────
    pctx, pt = new_ctx(browser, "patient", width=390)
    pctx.request.post("/api/auth/register", headers=H, data={"role": "patient", "full_name": "Prathna S", "phone": phone, "password": PW})
    time.sleep(0.3)
    code = re.findall(rf"code for {re.escape(phone)} \(verify_phone\): (\d{{6}})", open(LOG).read())[-1]
    check("patient phone verified", pctx.request.post("/api/auth/verify/confirm", headers=H, data={"channel": "phone", "code": code}).ok)
    pt.goto("/patient")
    pt.get_by_text("Dr. Asha Rao").click()
    pt.wait_for_url(re.compile(r"/patient/prescriptions/"))
    expect(pt.get_by_text("Get it from a pharmacy")).to_be_visible()
    check("patient sees the diagnosis", pt.get_by_text("AGE", exact=True).is_visible())
    pt.get_by_role("button", name="Share with a pharmacy").click()
    otp_el = pt.locator("p.data.text-5xl")
    expect(otp_el).to_be_visible()
    otp = re.sub(r"\D", "", otp_el.inner_text())
    check("6-digit code shown with a countdown", len(otp) == 6 and pt.get_by_text(re.compile(r"Use it within \d:\d\d")).is_visible(), otp)
    check("patient screen: no horizontal scroll at 390px", no_hscroll(pt))

    # ── pharmacy: waits for approval, then dispenses ──────────────────────
    cctx, c = new_ctx(browser, "chemist")
    cctx.request.post("/api/auth/register", headers=H, data={
        "role": "chemist", "full_name": "Ravi Kumar", "email": f"ravi{tag}@example.com", "password": PW,
        "profile": {"pharmacy_name": "Kumar Medicals", "pharmacy_address": "FC Road, Pune"}})
    c.goto("/pharmacy")
    expect(c.get_by_text("Your account is being verified")).to_be_visible()
    check("unapproved pharmacy sees the waiting screen, told the licence number is missing",
          c.get_by_text(re.compile("You have not given a drug licence number")).is_visible())
    pending = actx.request.get("/api/admin/accounts").json()
    actx.request.post(f"/api/admin/accounts/{pending[0]['id']}/vetting", headers=H, data={"decision": "approve"})
    c.get_by_role("button", name="Check again").click()
    c.get_by_label("The 6 digits on the patient’s phone").fill("000001" if otp != "000001" else "000002")
    c.get_by_role("button", name="Open prescription").click()
    expect(c.get_by_text(re.compile("not valid"))).to_be_visible()
    check("wrong code: inline error", True)
    c.get_by_label("The 6 digits on the patient’s phone").fill(f"{otp[:3]} {otp[3:]}")
    c.get_by_role("button", name="Open prescription").click()
    c.wait_for_url(re.compile(r"/pharmacy/grants/"))
    expect(c.get_by_role("timer")).to_contain_text("Access ends in")
    check("grant open with countdown", True)
    check("pharmacy never sees the phone", phone not in c.content())
    check("pharmacy is told the prescriber was reviewed", c.get_by_text("Registration details reviewed by a PrescriptAI administrator.").is_visible())

    c.get_by_role("tab", name="Type it").click()
    c.get_by_label("Brand", exact=True).fill("Dolo 650")
    c.get_by_role("button", name="Check this pack").click()
    expect(c.get_by_text("Wrong medicine", exact=True)).to_be_visible()
    check("typed Dolo 650 -> Wrong medicine, do not dispense", c.get_by_text("Do not dispense this pack").is_visible())

    c.get_by_role("tab", name="Scan QR").click()
    c.get_by_label("QR contents").fill("Brand Name: Cofarin 1mg\nBatch No: CF77\nExp: 10/2027")
    c.get_by_label("Quantity").fill("10 tabs")
    c.get_by_role("button", name="Check this pack").click()
    expect(c.get_by_text("Match", exact=True)).to_be_visible()
    check("QR Cofarin -> Match, recorded as dispensed", c.get_by_text("Recorded as dispensed.").is_visible())

    c.get_by_role("tab", name="Photo").click()
    expect(c.get_by_text("A photographed pack always reads as the same sample pack")).to_be_visible()
    check("photo tab warns about the demo pack reader", True)
    c.set_input_files("form input[type=file]", f"{TMP}/p1.png")
    c.get_by_role("button", name="Check this pack").click()
    expect(c.get_by_role("button", name="I have checked this. Dispense it.")).to_be_visible()
    c.get_by_role("button", name="I have checked this. Dispense it.").click()
    expect(c.get_by_text("Recorded as dispensed.")).to_have_count(2)
    check("orange result dispensed only after the chemist confirms", True)
    c.screenshot(path=f"{TMP}/shot_pharmacy.png", full_page=True)

    pt.reload()
    expect(pt.get_by_text("Shared with Kumar Medicals until")).to_be_visible()
    check("patient: 'Shared with Kumar Medicals until …'", True)
    check("patient: dispensed medicines ticked", pt.get_by_text("Dispensed", exact=True).count() == 2)
    pt.get_by_role("button", name="End access now").click()
    expect(pt.get_by_role("button", name="Share with a pharmacy")).to_be_visible()
    check("patient revoked access", True)
    check("patient detail: no horizontal scroll at 390px", no_hscroll(pt))

    c.reload()
    expect(c.get_by_role("heading", name="Access ended")).to_be_visible()
    check("pharmacy: access ended after revoke", "Ms. Prathna" not in c.content())
    c.goto("/pharmacy/records")
    expect(c.locator("tbody tr")).to_have_count(2)
    check("dispensing record: 2 rows", True)

    # ── public verify (phone width) ───────────────────────────────────────
    vctx, v = new_ctx(browser, "anon", width=390)
    v.goto(f"/check/{vid}")
    expect(v.get_by_text("Genuine and current")).to_be_visible()
    check("public verify page: genuine, no patient, says the doctor was reviewed",
          "Prathna" not in v.content() and "Cofarin" not in v.content()
          and v.get_by_text(re.compile("administrator reviewed this doctor")).is_visible())
    v.goto("/check/PRX-AAAAA-BBBBB")
    expect(v.get_by_text("Not recognised")).to_be_visible()
    check("public verify page: unknown ID", True)
    check("verify page: no horizontal scroll at 390px", no_hscroll(v))

    # the admin's own account page (where the review screen sends them for two-step verification)
    a.get_by_role("link", name="Turn it on").click()
    a.wait_for_url(re.compile(r"/account$"))
    expect(a.get_by_text("Administrator account")).to_be_visible()
    check("admin: the account page opens (two-step verification is set up there)",
          a.get_by_text(re.compile("Two-step verification", re.I)).first.is_visible())

    # admin at phone width
    a.set_viewport_size({"width": 390, "height": 800})
    a.goto("/admin")
    expect(a.get_by_role("heading", name="Verify accounts")).to_be_visible()
    check("admin page: no horizontal scroll at 390px", no_hscroll(a))

    browser.close()

# 4xx responses are deliberate here (wrong code, duplicate upload, rejection without a reason).
expected = re.compile(r"status of (4(0[0-9]|1[0-9]|22|29))")
for who, errs in console.items():
    real = [e for e in errs if not expected.search(e)]
    check(f"no unexpected console errors ({who})", not real, real[:3])

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
for n in failed:
    print("  FAILED:", n)
sys.exit(1 if failed else 0)
