"""
End-to-end API checks against a LIVE server: Workflow stages 1-7, vetting,
multi-page and duplicate handling, and the security rules around them.

Run it through the harness, which starts a throwaway server for it:

    python tests/run.py api

It needs (from the harness, via the environment): E2E_BASE, E2E_LOG (the server
log, where verification codes are printed), E2E_DB (the SQLite file, to move
clocks), E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD, and E2E_STATE (a file it writes
for the after-restart checks). Mock reader, fixture medicine data: no API spend.

Against a server in a container, also set E2E_SQL_CMD: a command that runs the
SQL given as its last argument inside that container. A write made from the
host through a shared folder is not reliably seen by SQLite on the other side.
"""

import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time

import httpx
from PIL import Image

BASE = os.environ["E2E_BASE"]
LOG = os.environ["E2E_LOG"]
DB = os.environ["E2E_DB"]
ADMIN = (os.environ["E2E_ADMIN_EMAIL"], os.environ["E2E_ADMIN_PASSWORD"])
STATE = os.environ.get("E2E_STATE")
H = {"X-Requested-With": "PrescriptAI"}
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{str(extra)[:300]}]" if (extra != "" and not cond) else ""))


def client():
    return httpx.Client(base_url=BASE, headers=H, timeout=120)


def png(color, size=(600, 800)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def jpeg(color, size=(300, 400)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "JPEG")
    return b.getvalue()


def code_for(dest):
    time.sleep(0.3)
    found = re.findall(rf"code for {re.escape(dest)} \(verify_phone\): (\d{{6}})", open(LOG).read())
    return found[-1] if found else None


def sql_write(statement):
    """Move a clock in the server's database (expiry cannot be waited for)."""
    cmd = os.environ.get("E2E_SQL_CMD")
    if cmd:
        subprocess.run(shlex.split(cmd) + [statement], check=True, capture_output=True)
    else:
        with sqlite3.connect(DB) as w:
            w.execute(statement)


def section(title):
    print(f"\n── {title} " + "─" * max(4, 66 - len(title)))


tag = str(int(time.time()))[-6:]
PW = "Correct-Horse-9-Battery"

# ═══ accounts ═══════════════════════════════════════════════════════════════
section("accounts")


def register(role, name, **kw):
    c = client()
    r = c.post("/api/auth/register", json={"role": role, "full_name": name, "password": PW, **kw})
    return c, r


doc, r = register("doctor", "Dr. Asha Rao", email=f"asha{tag}@example.com",
                  profile={"speciality": "General Medicine", "clinic_address": "12 MG Road, Pune",
                           "registration_no": "MMC-2011-0457"})
check("doctor registers", r.status_code == 201, r.text)
doc2, _ = register("doctor", "Dr. Other", email=f"other{tag}@example.com",
                   profile={"speciality": "ENT", "clinic_address": "Elsewhere", "registration_no": "MMC-2"})
doc3, _ = register("doctor", "Dr. Quota", email=f"quota{tag}@example.com",
                   profile={"speciality": "GP", "clinic_address": "Third St", "registration_no": "MMC-3"})
phone = f"+9198{tag}11"
pat, r = register("patient", "Prathna S", phone=phone)
check("patient registers by phone", r.status_code == 201, r.text)
pat2, _ = register("patient", "Someone Else", phone=f"+9197{tag}22")
chem, r = register("chemist", "Ravi Kumar", email=f"ravi{tag}@example.com",
                   profile={"pharmacy_name": "Kumar Medicals", "pharmacy_address": "FC Road, Pune",
                            "drug_licence_no": "MH-PZ4-112233"})
check("pharmacy registers", r.status_code == 201, r.text)
chem2, _ = register("chemist", "Other Chemist", email=f"chem2{tag}@example.com",
                    profile={"pharmacy_name": "Other Pharma", "pharmacy_address": "Somewhere"})

# ═══ vetting ════════════════════════════════════════════════════════════════
section("vetting: nobody is a doctor until an administrator says so")

me = doc.get("/api/auth/me").json()
check("a new doctor is pending and cannot practise", me["vetting_status"] == "pending" and me["can_practise"] is False, me)
r = doc.post("/api/consultations", json={"patient_name": "X", "patient_phone": phone})
check("pending doctor: 403 on creating a consultation", r.status_code == 403 and "waiting to be verified" in r.text, r.text)
check("pending doctor: 403 on stats and uploads too",
      doc.get("/api/stats").status_code == 403 and doc.get("/uploads/x.png").status_code == 403)
check("pending pharmacy: 403 on redeeming a code", chem.post("/api/pharmacy/redeem", json={"otp": "123456"}).status_code == 403)
me = pat.get("/api/auth/me").json()
check("patients are not vetted", me["vetting_status"] is None and me["can_practise"] is True)
_, r = register("admin", "Evil Admin", email=f"evil{tag}@example.com")
check("nobody can register as an administrator", r.status_code == 403, r.text)
check("a doctor cannot call the admin API", doc.get("/api/admin/accounts").status_code == 403)
check("the admin API needs a session", client().get("/api/admin/accounts").status_code == 401)

admin = client()
r = admin.post("/api/auth/login", json={"identifier": ADMIN[0], "password": ADMIN[1]})
check("administrator (created by manage.py) signs in", r.status_code == 200 and r.json()["status"] == "ok", r.text)
pending = admin.get("/api/admin/accounts").json()
by_name = {p["full_name"]: p for p in pending}
check("admin sees the 5 waiting accounts with their numbers",
      len(pending) == 5 and by_name["Dr. Asha Rao"]["profile"]["registration_no"] == "MMC-2011-0457", [p["full_name"] for p in pending])


def vet(name, decision, note=None):
    return admin.post(f"/api/admin/accounts/{by_name[name]['id']}/vetting", json={"decision": decision, "note": note})


check("rejecting needs a reason", vet("Dr. Other", "reject").status_code == 422)
r = vet("Dr. Other", "reject", "Registration number not found on the council register.")
check("reject", r.json()["vetting_status"] == "rejected")
r = doc2.get("/api/consultations")
me = doc2.get("/api/auth/me").json()
check("rejected doctor: 403, and sees the reason", r.status_code == 403 and "not approved" in r.text
      and "council register" in (me["vetting_note"] or ""), r.text)
for name in ("Dr. Asha Rao", "Dr. Other", "Dr. Quota", "Ravi Kumar", "Other Chemist"):
    vet(name, "approve", "Checked.")
check("approve: summary shows 5 approved, 0 waiting",
      admin.get("/api/admin/summary").json() == {"pending": 0, "approved": 5, "rejected": 0})
check("approved doctor can practise", doc.get("/api/auth/me").json()["can_practise"] is True
      and doc2.get("/api/consultations").status_code == 200)

r = doc2.patch("/api/auth/me", json={"profile": {"clinic_address": "A New Clinic"}})
check("changing the clinic keeps the approval", r.json()["vetting_status"] == "approved")
r = doc2.patch("/api/auth/me", json={"profile": {"registration_no": "MMC-SOMEONE-ELSE"}})
check("changing the registration number sends the account back for review",
      r.json()["vetting_status"] == "pending" and doc2.get("/api/consultations").status_code == 403)
vet("Dr. Other", "approve")

# ═══ stage 1: consultation ══════════════════════════════════════════════════
section("stage 1: consultation")
r = doc.post("/api/consultations", json={"patient_name": "Ms. Prathna", "patient_phone": phone[3:]})
check("consultation created (bare 10-digit phone normalised)", r.status_code == 201 and r.json()["patient_phone"] == phone, r.text)
C = r.json()
cid = C["id"]
check("clinic defaults from doctor profile", C["clinic_address"] == "12 MG Road, Pune")
check("starts as draft, version 0", C["status"] == "draft" and C["current_version"] == 0)
check("reader reported as mock", C["reader_is_mock"] is True)
check("other doctor gets 404", doc2.get(f"/api/consultations/{cid}").status_code == 404)
check("patient gets 403 on doctor route", pat.get(f"/api/consultations/{cid}").status_code == 403)
check("anonymous gets 401", client().get(f"/api/consultations/{cid}").status_code == 401)
check("CSRF header required", httpx.post(f"{BASE}/api/consultations", json={}, cookies=doc.cookies).status_code == 403)

# ═══ stage 2: pages ═════════════════════════════════════════════════════════
section("stage 2: pages")
r = doc.post(f"/api/consultations/{cid}/pages", files={"image": ("p1.png", png("white"), "image/png")})
check("page 1 uploaded", r.status_code == 200, r.text[:300])
C = r.json()
check("diagnosis read from page (verbatim)", C["diagnosis"] == "AGE" and C["diagnosis_as_read"] == ["AGE"])
outs = [m["outcome"] for m in C["medicines"]]
check("mock page: 3 lines incl. an illegible one", len(outs) == 3 and "illegible" in outs, outs)
r = doc.post(f"/api/consultations/{cid}/pages", files={"image": ("p2.png", png("ivory"), "image/png")})
C = r.json()
check("page 2 uploaded -> 2 pages, 6 lines", len(C["pages"]) == 2 and len(C["medicines"]) == 6)
check("other doctor cannot read the image", doc2.get(C["pages"][0]["image_url"]).status_code == 404)
check("owner can read the image", doc.get(C["pages"][0]["image_url"]).status_code == 200)
p2 = C["pages"][1]["id"]
r = doc.delete(f"/api/consultations/{cid}/pages/{p2}")
check("delete a page before first finalise", r.status_code == 200 and len(r.json()["pages"]) == 1)
C = r.json()
p1 = C["pages"][0]["id"]

# ═══ stage 3: review ════════════════════════════════════════════════════════
section("stage 3: review and finalise")
r = doc.post(f"/api/consultations/{cid}/finalise", json={})
check("finalise blocked while a line is unresolved", r.status_code == 422 and "unresolved" in r.text, r.text)

illegible = next(m for m in C["medicines"] if m["outcome"] == "illegible")
r = doc.post(f"/api/prescriptions/{p1}/corrections", json={
    "medicine_index": illegible["index"], "field": "brand", "corrected_value": "Zofer 4mg",
    "correction_type": "misread"})
m = r.json().get("medicine", {})
check("brand correction re-resolves composition (S3)",
      m.get("brand") == "Zofer 4mg" and m.get("ingredients") == ["Ondansetron"] and m.get("outcome") == "confirmed", m)
r = doc.post(f"/api/prescriptions/{p1}/corrections", json={
    "medicine_index": 0, "field": "dosage", "corrected_value": "1 tab", "correction_type": "missed"})
check("dosage correction", r.status_code == 200)
r = doc.post(f"/api/prescriptions/{p1}/corrections", json={
    "medicine_index": 0, "field": "ingredients", "corrected_value": "x"})
check("correction refuses a non-editable field", r.status_code == 422)

r = doc.post(f"/api/consultations/{cid}/medicines", json={"name": "Cofarin 1mg", "frequency": "0-0-1"})
check("add missed medicine (Warfarin)", r.status_code == 200 and any(x["brand"] == "Cofarin 1mg" for x in r.json()["medicines"]))
r = doc.post(f"/api/consultations/{cid}/medicines", json={"name": "ALFAM 400MG", "frequency": "1-0-1"})
C = r.json()
check("warfarin + ibuprofen across the consultation -> severe", C["interactions"]["overall_risk"] == "severe")
r = doc.post(f"/api/consultations/{cid}/medicines", json={"name": "Zofer"})
z = r.json()["medicines"][-1]
r = doc.post(f"/api/prescriptions/{p1}/medicines/{z['index']}/remove", json={"note": "added by mistake"})
check("strike a line", r.status_code == 200 and r.json()["medicines"][-1]["removed"] is True)
r = doc.patch(f"/api/consultations/{cid}", json={"diagnosis": "Acute gastroenteritis"})
check("diagnosis edited", r.json()["diagnosis"] == "Acute gastroenteritis")
check("patient sees nothing before finalise", pat.get(f"/api/patient/prescriptions/{cid}").status_code == 404)

r = doc.post(f"/api/consultations/{cid}/finalise", json={})
check("finalise v1", r.status_code == 200 and r.json()["current_version"] == 1 and r.json()["status"] == "finalised", r.text[:300])
C = r.json()
vid1 = C["versions"][0]["verification_id"]
check("finalise again with no change -> 409", doc.post(f"/api/consultations/{cid}/finalise", json={}).status_code == 409)

v = client().get(f"/api/verify/{vid1}").json()
check("public verify: valid, doctor, reg no, reviewed, no patient data",
      v["valid"] and v["doctor_name"] == "Dr. Asha Rao" and v["registration_no"] == "MMC-2011-0457"
      and v["doctor_vetted"] is True and "prathna" not in str(v).lower() and "warfarin" not in str(v).lower(), v)
check("public verify: unknown id -> invalid", client().get("/api/verify/PRX-AAAAA-BBBBB").json()["valid"] is False)

# ═══ stage 4: patient ═══════════════════════════════════════════════════════
section("stage 4: patient")
inbox = pat.get("/api/patient/prescriptions").json()
check("unverified phone -> empty inbox", inbox["phone_verified"] is False and inbox["prescriptions"] == [])
check("unverified phone -> 404 on detail", pat.get(f"/api/patient/prescriptions/{cid}").status_code == 404)
r = pat.post("/api/auth/verify/confirm", json={"channel": "phone", "code": code_for(phone)})
check("patient verifies phone", r.status_code == 200, r.text)
inbox = pat.get("/api/patient/prescriptions").json()
check("inbox lists the finalised prescription", [p["id"] for p in inbox["prescriptions"]] == [cid], inbox)
P = pat.get(f"/api/patient/prescriptions/{cid}").json()
check("patient view: v1, not revised, diagnosis shown", P["version"] == 1 and not P["revised"] and P["diagnosis"] == "Acute gastroenteritis")
check("patient view: struck line absent", len(P["medicines"]) == 5)
check("patient view: no safer_alternative", "safer_alternative" not in str(P) and "mechanism" not in str(P))
check("patient view: interactions advise discussing", P["interactions"] and all("doctor or pharmacist" in i["advice"] for i in P["interactions"]))
check("patient view: never the phone", phone not in str(P))
check("other patient -> 404", pat2.get(f"/api/patient/prescriptions/{cid}").status_code == 404)
img = pat.get(f"/api/patient/prescriptions/{cid}/pages/1/image")
check("patient page image, no-store", img.status_code == 200 and img.headers.get("cache-control") == "private, no-store")
pdf = pat.get(f"/api/patient/prescriptions/{cid}/pdf")
check("PDF downloads", pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and vid1 in pdf.headers.get("content-disposition", ""))

# ═══ stage 5: sharing ═══════════════════════════════════════════════════════
section("stage 5: sharing by one-time code")
r = pat.post(f"/api/patient/prescriptions/{cid}/share")
check("patient issues OTP", r.status_code == 200 and re.fullmatch(r"\d{6}", r.json()["otp"]), r.text)
first_otp = r.json()["otp"]
otp = pat.post(f"/api/patient/prescriptions/{cid}/share").json()["otp"]
check("new OTP cancels the old one", first_otp == otp or chem.post("/api/pharmacy/redeem", json={"otp": first_otp}).status_code == 400)
check("other patient cannot share it", pat2.post(f"/api/patient/prescriptions/{cid}/share").status_code == 404)
check("doctor cannot redeem (403)", doc.post("/api/pharmacy/redeem", json={"otp": otp}).status_code == 403)
bad = "000000" if otp != "000000" else "111111"
r = chem.post("/api/pharmacy/redeem", json={"otp": bad})
check("wrong OTP -> 400, generic message", r.status_code == 400 and "not valid" in r.text)
r = chem.post("/api/pharmacy/redeem", json={"otp": otp})
check("pharmacy redeems", r.status_code == 200 and r.json()["status"] == "active", r.text)
gid = r.json()["id"]
check("redeem is single-use (other pharmacy)", chem2.post("/api/pharmacy/redeem", json={"otp": otp}).status_code == 400)
G = chem.get(f"/api/pharmacy/grants/{gid}").json()
check("pharmacy view: medicines + doctor + patient name", len(G["medicines"]) == 5 and G["doctor"]["name"] == "Dr. Asha Rao" and G["patient_name"] == "Ms. Prathna")
check("pharmacy view: doctor shown as reviewed", G["doctor"]["vetted"] is True)
check("pharmacy view: never the phone", phone not in str(G))
check("pharmacy view: diagnosis hidden by default", G["diagnosis"] is None)
check("pharmacy view: ~15 min window", 14 * 60 <= G["seconds_left"] <= 15 * 60)
check("other pharmacy -> 404 on the grant", chem2.get(f"/api/pharmacy/grants/{gid}").status_code == 404)
check("pharmacy page image during grant", chem.get(f"/api/pharmacy/grants/{gid}/pages/1/image").status_code == 200)
P = pat.get(f"/api/patient/prescriptions/{cid}").json()
active = [s for s in P["shares"] if s["status"] == "active"]
check("patient sees 'shared with Kumar Medicals'", active and active[0]["pharmacy_name"] == "Kumar Medicals")

# ═══ stage 6: pack checks ═══════════════════════════════════════════════════
section("stage 6: pack checks")


def pack(**form):
    files = form.pop("files", None)
    return chem.post(f"/api/pharmacy/grants/{gid}/checks", data=form, files=files)


c = pack(method="manual", brand="Dolo 650").json()["checks"][-1]
check("Dolo 650 (paracetamol alone) vs Azenac MR -> wrong medicine, blocked", c["result"] == "wrong_medicine" and c["blocked"], c)
c = pack(method="qr", qr_text="Brand Name: Cofarin 1mg\nBatch No: CF77\nExp: 10/2027", quantity="10").json()["checks"][-1]
check("QR Cofarin 1mg -> match, dispensed, batch kept", c["result"] == "match" and c["dispensed"] and c["batch"] == "CF77", c)
c = pack(method="manual", brand="Brufen 400").json()["checks"][-1]
check("Brufen 400 for ALFAM 400MG -> substitute (same ibuprofen 400)", c["result"] == "substitute" and c["dispensed"], c)
c = pack(method="photo", files={"image": ("pack.png", png("gray"), "image/png")}).json()["checks"][-1]
check("photo via mock reader -> labelled demo, needs confirmation",
      c["reader"] == "mock-pack" and any("Demo reader" in x for x in c["reasons"]) and c["needs_confirmation"] and not c["dispensed"], c)
G = chem.post(f"/api/pharmacy/grants/{gid}/checks/{c['id']}/confirm").json()
check("chemist confirms -> dispensed", G["checks"][-1]["dispensed"])
blocked = next(x for x in G["checks"] if x["result"] == "wrong_medicine")
check("cannot confirm a red result", chem.post(f"/api/pharmacy/grants/{gid}/checks/{blocked['id']}/confirm").status_code == 409)
check("partial dispensing state", G["dispensing_state"] == "partial", G["dispensing_state"])
check("URL-only QR -> could not verify", pack(method="qr", qr_text="https://example.com/p/1").json()["checks"][-1]["result"] == "could_not_verify")
recs = chem.get("/api/pharmacy/records").json()
check("dispensing records: 3 rows, minimal fields only",
      len(recs) == 3 and all(set(x) == {"id", "date", "prescription_id", "prescriber_name", "prescriber_registration_no",
                                        "brand", "composition", "strength", "batch", "expiry", "quantity", "result"} for x in recs)
      and all(x["prescription_id"] == vid1 for x in recs), recs)
P = pat.get(f"/api/patient/prescriptions/{cid}").json()
check("patient sees the dispensing checks", len(P["dispensing"]) == 5 and sum(m["dispensed"] for m in P["medicines"]) == 3)

# ═══ revisions ══════════════════════════════════════════════════════════════
section("revisions")
doc.post(f"/api/prescriptions/{p1}/corrections", json={
    "medicine_index": 0, "field": "duration", "corrected_value": "5 days", "correction_type": "wrong_field"})
check("edit after finalise -> revising", doc.get(f"/api/consultations/{cid}").json()["status"] == "revising")
P = pat.get(f"/api/patient/prescriptions/{cid}").json()
check("patient still sees v1 while doctor edits", P["version"] == 1 and P["medicines"][0]["duration"] == "3 days")
r = doc.post(f"/api/consultations/{cid}/finalise", json={"change_note": "Oflazest OZ now for 5 days"})
check("finalise v2", r.json()["current_version"] == 2)
P = pat.get(f"/api/patient/prescriptions/{cid}").json()
check("patient sees v2, labelled revised", P["version"] == 2 and P["revised"] and P["medicines"][0]["duration"] == "5 days" and P["change_note"])
v = client().get(f"/api/verify/{vid1}").json()
check("v1 verification now reports superseded", v["superseded"] and v["latest_version"] == 2)
G = chem.get(f"/api/pharmacy/grants/{gid}").json()
check("pharmacy sees latest (v2)", G["version"] == 2 and G["revised"])
check("dispensing carries over to v2 (a duration change un-dispenses nothing)", sum(m["dispensed"] for m in G["medicines"]) == 3)
c = pack(method="qr", qr_text="Brand Name: Cofarin 1mg").json()["checks"][-1]
check("second pack of a dispensed medicine asks for confirmation", c["needs_confirmation"] and not c["dispensed"], c)
doc.patch(f"/api/consultations/{cid}", json={"diagnosis_visibility": "patient_and_pharmacy"})
doc.post(f"/api/consultations/{cid}/finalise", json={})
check("diagnosis visible to pharmacy only when the doctor allows it",
      chem.get(f"/api/pharmacy/grants/{gid}").json()["diagnosis"] == "Acute gastroenteritis")

# ═══ stage 7: revoke + expiry ═══════════════════════════════════════════════
section("stage 7: revoke and expiry")
check("patient revokes", pat.post(f"/api/patient/grants/{gid}/revoke").json()["status"] == "revoked")
check("revoked -> 410 on read", chem.get(f"/api/pharmacy/grants/{gid}").status_code == 410)
check("revoked -> 410 on image", chem.get(f"/api/pharmacy/grants/{gid}/pages/1/image").status_code == 410)
check("revoked -> 410 on check", pack(method="manual", brand="Dolo 650").status_code == 410)
lst = chem.get("/api/pharmacy/grants").json()
check("ended grant lists no patient details", lst[0]["status"] == "revoked" and lst[0]["patient_name"] is None)
check("records survive the end of access", len(chem.get("/api/pharmacy/records").json()) == 3)

otp = pat.post(f"/api/patient/prescriptions/{cid}/share").json()["otp"]
gid2 = chem.post("/api/pharmacy/redeem", json={"otp": otp}).json()["id"]
con = sqlite3.connect(DB)
assert re.fullmatch(r"[0-9a-f-]{36}", gid2)
sql_write(f"UPDATE access_grants SET expires_at = '2020-01-01 00:00:00' WHERE id = '{gid2}'")
check("expired window -> 410, enforced by the server", chem.get(f"/api/pharmacy/grants/{gid2}").status_code == 410)
otp = pat.post(f"/api/patient/prescriptions/{cid}/share").json()["otp"]
sql_write("UPDATE access_grants SET redeem_by = '2020-01-01 00:00:00' WHERE redeemed_at IS NULL AND cancelled_at IS NULL")
check("unredeemed code lapses after the redeem window", chem.post("/api/pharmacy/redeem", json={"otp": otp}).status_code == 400)

# a pharmacy whose approval is withdrawn loses an open grant on its next request
otp = pat.post(f"/api/patient/prescriptions/{cid}/share").json()["otp"]
gid3 = chem.post("/api/pharmacy/redeem", json={"otp": otp}).json()["id"]
check("grant open before approval is withdrawn", chem.get(f"/api/pharmacy/grants/{gid3}").status_code == 200)
vet("Ravi Kumar", "reject", "Licence suspended.")
check("approval withdrawn -> the pharmacy is cut off mid-grant", chem.get(f"/api/pharmacy/grants/{gid3}").status_code == 403)
vet("Ravi Kumar", "approve")

for i in range(6):
    r = chem2.post("/api/pharmacy/redeem", json={"otp": f"{(123450 + i):06d}"})
check("wrong codes lock the pharmacy account (429)", r.status_code == 429, r.status_code)

other_phone = pat2.get("/api/auth/me").json()["phone"]
doc.patch(f"/api/consultations/{cid}", json={"patient_phone": other_phone})
check("re-address not finalised -> original patient keeps access", pat.get(f"/api/patient/prescriptions/{cid}").status_code == 200)
doc.post(f"/api/consultations/{cid}/finalise", json={})
check("re-address finalised -> original patient loses access", pat.get(f"/api/patient/prescriptions/{cid}").status_code == 404)
check("...and it leaves the original inbox", pat.get("/api/patient/prescriptions").json()["prescriptions"] == [])

s = doc.get("/api/stats").json()
check("stats: consultations + finalised", s["consultations"] == 1 and s["finalised"] == 1, s)
types = {x["correction_type"] for x in doc.get("/api/corrections").json()}
check("corpus has misread / missed / hallucinated / wrong_field", {"misread", "missed", "hallucinated", "wrong_field"} <= types, types)
check("other doctor sees none of the corpus", doc2.get("/api/corrections").json() == [])

# ═══ API calling: multiple pages and duplicates ═════════════════════════════
section("API calling: multiple pages and duplicates")
r = doc.post("/api/prescriptions/upload", files={"image": ("x.png", png("white"), "image/png")})
check("legacy single-page upload -> wrapped in a consultation", r.status_code == 200 and r.json()["consultation_id"], r.text[:200])
row = con.execute("SELECT from_cache, cost_usd FROM prescriptions WHERE id = ?", (r.json()["id"],)).fetchone()
check("the same photo in another consultation comes from the read cache (no second reader call)", row == (1, 0.0), row)

c4 = doc.post("/api/consultations", json={"patient_name": "Batch Patient", "patient_phone": "+919000000004"}).json()["id"]
A, B = png("white"), png("lavender", (640, 820))
r = doc.post(f"/api/consultations/{c4}/pages/batch", files=[
    ("images", ("a.png", A, "image/png")), ("images", ("b.png", B, "image/png")),
    ("images", ("a-again.png", A, "image/png")), ("images", ("notes.txt", b"not an image", "text/plain")),
    ("images", ("evil.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml"))])
out = r.json()
st = {x["filename"]: x for x in out["results"]}
check("batch: one request, one result per file", r.status_code == 200 and len(out["results"]) == 5, r.text[:300])
check("batch: two distinct pages added in order", st["a.png"]["status"] == "added" and st["a.png"]["page_no"] == 1
      and st["b.png"]["page_no"] == 2 and len(out["consultation"]["pages"]) == 2)
check("batch: the same image twice in one upload is read once", st["a-again.png"]["status"] == "duplicate" and "a.png" in st["a-again.png"]["detail"])
check("batch: a non-image and an SVG fail on their own, without losing the others",
      st["notes.txt"]["status"] == "failed" and st["evil.svg"]["status"] == "failed")
r = doc.post(f"/api/consultations/{c4}/pages", files={"image": ("a3.png", A, "image/png")})
check("the same image again in the same prescription -> 409 before any reading", r.status_code == 409 and "already page 1" in r.text, r.text)
r = doc.post(f"/api/consultations/{c4}/pages/batch", files=[("images", ("b2.png", B, "image/png"))])
check("batch of only a duplicate: nothing added, told which page it already is",
      r.json()["results"][0]["status"] == "duplicate" and "page 2" in r.json()["results"][0]["detail"] and len(r.json()["consultation"]["pages"]) == 2)

D = doc.get(f"/api/consultations/{c4}").json()["duplicates"]
kinds = {d["kind"] for d in D}
check("two pages that READ the same are flagged", any(d["kind"] == "same_reading" and d["page_no"] == 2 and d["other_page_no"] == 1 for d in D), D)
check("the same photo in another of this doctor's consultations is pointed out",
      "same_image_elsewhere" in kinds and any(d.get("other_consultation_id") == cid for d in D), D)
c5 = doc2.post("/api/consultations", json={"patient_name": "Doc2 Patient", "patient_phone": "+919000000005"}).json()["id"]
r = doc2.post(f"/api/consultations/{c5}/pages", files={"image": ("w.png", A, "image/png")})
check("...but never across doctors (no hint another doctor holds this photo)", r.json()["duplicates"] == [], r.json()["duplicates"])

r = doc.post(f"/api/consultations/{c4}/finalise", json={"acknowledge_unresolved": True})
check("finalise refuses while two pages read identically", r.status_code == 422 and "read identically" in r.text, r.text)
r = doc.post(f"/api/consultations/{c4}/finalise", json={"acknowledge_unresolved": True, "acknowledge_duplicates": True})
check("...and accepts once the doctor says both pages are meant", r.status_code == 200 and r.json()["current_version"] == 1, r.text[:200])

c6 = doc.post("/api/consultations", json={"patient_name": "Too Many", "patient_phone": "+919000000006"}).json()["id"]
many = [("images", (f"p{i}.png", png("white", (500 + i, 700)), "image/png")) for i in range(11)]
check("more than 10 pages in one prescription is refused", doc.post(f"/api/consultations/{c6}/pages/batch", files=many).status_code == 422)

# per-doctor daily ceiling on reader calls (the harness sets it to 15)
qa = doc3.post("/api/consultations", json={"patient_name": "Q A", "patient_phone": "+919000000007"}).json()["id"]
ten = [("images", (f"q{i}.png", png("beige", (520 + i, 700)), "image/png")) for i in range(10)]
r = doc3.post(f"/api/consultations/{qa}/pages/batch", files=ten)
check("10 distinct pages read in one batch", r.status_code == 200 and all(x["status"] == "added" for x in r.json()["results"]), r.text[:200])
qb = doc3.post("/api/consultations", json={"patient_name": "Q B", "patient_phone": "+919000000008"}).json()["id"]
six = [("images", (f"r{i}.png", png("linen", (540 + i, 700)), "image/png")) for i in range(6)]
r = doc3.post(f"/api/consultations/{qb}/pages/batch", files=six)
check("the per-doctor daily read ceiling stops the 16th page (429)", r.status_code == 429, r.status_code)
check("...for that doctor only", doc.post(f"/api/consultations/{c6}/pages", files={"image": ("ok.png", png("azure"), "image/png")}).status_code == 200)

# ═══ uploads cannot become scripts ══════════════════════════════════════════
section("upload safety and headers")
c3 = doc.post("/api/consultations", json={"patient_name": "X", "patient_phone": "+919000000001"}).json()["id"]
r = doc.post(f"/api/consultations/{c3}/pages", files={"image": ("rx.html", jpeg("white"), "image/jpeg")})
url = r.json()["pages"][0]["image_url"]
check("JPEG named rx.html is stored as .jpg", url.endswith(".jpg"), url)
img = doc.get(url)
check("served as image/jpeg with nosniff + sandbox CSP",
      img.headers["content-type"] == "image/jpeg" and img.headers.get("x-content-type-options") == "nosniff"
      and "sandbox" in img.headers.get("content-security-policy", ""))
r = doc.post(f"/api/consultations/{c3}/pages", files={"image": ("x.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")})
check("SVG / non-image payload refused", r.status_code == 400, r.status_code)
r = doc.post(f"/api/consultations/{c3}/pages", files={"image": ("x./../../escaped", jpeg("ivory"), "image/jpeg")})
check("path in filename cannot escape uploads/", r.status_code == 200 and r.json()["pages"][-1]["image_url"].count("/") == 2)
h = client().get("/api/health")
check("security headers on every response", h.headers.get("x-frame-options") == "DENY" and "frame-ancestors 'none'" in h.headers.get("content-security-policy", ""))
check("no HSTS on plain http (it is sent once COOKIE_SECURE is on)", "strict-transport-security" not in h.headers)
check("health reports how codes travel and that vetting is on",
      h.json()["phone_codes_by"] == "console" and h.json()["vetting_required"] is True and h.json()["reader_check"] == "not_run")

# two-step secret is not readable in the database
r = doc.post("/api/auth/mfa/setup")
secret = r.json()["secret"]
row = con.execute("SELECT mfa_pending_secret FROM users WHERE email = ?", (f"asha{tag}@example.com",)).fetchone()
check("a two-step secret is ciphertext in the database", row[0].startswith("enc:v1:") and secret not in row[0], row[0][:16])

# approval withdrawn: the doctor is cut off at once, with a live session
vet("Dr. Quota", "reject", "Withdrawn for the test.")
check("approval withdrawn -> the doctor's very next request is refused", doc3.get("/api/consultations").status_code == 403)

if STATE:
    json.dump({"chem2": f"chem2{tag}@example.com", "password": PW}, open(STATE, "w"))

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
for n in failed:
    print("  FAILED:", n)
sys.exit(1 if failed else 0)
