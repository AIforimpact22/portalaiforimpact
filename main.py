import os
import secrets
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_from_directory, session
)
from werkzeug.utils import secure_filename

from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Numeric, ForeignKey,
    Text, func, select, UniqueConstraint, inspect, text
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, scoped_session

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

# -------------------------------------------------
# Config & helpers
# -------------------------------------------------
load_dotenv()  # loads local .env for DATABASE_URL, SECRET_KEY, etc.
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-" + secrets.token_hex(16))

app = Flask(__name__, template_folder="templates", static_folder=None)
app.config.update(SECRET_KEY=SECRET_KEY, UPLOAD_FOLDER=UPLOAD_FOLDER, MAX_CONTENT_LENGTH=25 * 1024 * 1024)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True))
Base = declarative_base()

# -------------------------------------------------
# DEFAULT COMPANY INFO (your data)
# -------------------------------------------------
DEFAULT_COMPANY = {
    "company_name": "Climate Resilience Fundraising Platform B.V.",
    "address": "Fluwelen Burgwal",
    "postcode": "2511CJ",
    "city": "Den Haag",
    "country": "Netherlands",
    "kvk": "94437289",
    "rsin": "866777398",
    # Use your real VAT number when available; placeholder warns in UI
    "vat_number": "NL[xxxx.xxx].B01",
    "iban": "NL06 REVO 7487 2866 30",
    "bic": "REVONL22",
    "invoice_prefix": "INV"
}

# -------------------------------------------------
# Models
# -------------------------------------------------
class CompanySettings(Base):
    __tablename__ = "company_settings"
    id = Column(Integer, primary_key=True)
    company_name = Column(String(160), nullable=False, default="")
    address = Column(Text, default="")
    kvk = Column(String(32), default="")
    rsin = Column(String(32), default="")
    vat_number = Column(String(32), default="")
    iban = Column(String(64), default="")
    bic = Column(String(32), default="")
    invoice_prefix = Column(String(16), default="INV")
    city = Column(String(80), default="")
    postcode = Column(String(24), default="")
    country = Column(String(80), default="Netherlands")

class InvoiceSequence(Base):
    __tablename__ = "invoice_sequences"
    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    prefix = Column(String(16), nullable=False, default="INV")
    last_seq = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint('year', 'prefix', name='uq_year_prefix'),)

class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    invoice_no = Column(String(64), unique=True, nullable=False)
    issue_date = Column(Date, nullable=False, default=date.today)
    supply_date = Column(Date, nullable=False, default=date.today)  # performance date
    due_date = Column(Date, nullable=False)
    currency = Column(String(8), nullable=False, default="EUR")

    # Customer details (Dutch invoice rules)
    client_name = Column(String(160), nullable=False)
    client_address = Column(Text, default="")
    client_vat_number = Column(String(40), default="")  # required if reverse charge

    # VAT scheme for the invoice header (applies alongside line VAT)
    # STANDARD / REVERSE_CHARGE_EU / ZERO_OUTSIDE_EU / EXEMPT
    vat_scheme = Column(String(32), nullable=False, default="STANDARD")

    notes = Column(Text, default="")
    status = Column(String(16), nullable=False, default="SENT")  # DRAFT/SENT/PARTIAL/PAID

    net_total = Column(Numeric(12, 2), nullable=False, default=0)
    vat_total = Column(Numeric(12, 2), nullable=False, default=0)
    gross_total = Column(Numeric(12, 2), nullable=False, default=0)

    lines = relationship("InvoiceLine", back_populates="invoice", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="invoice", cascade="all, delete-orphan")

    @property
    def paid_total(self) -> Decimal:
        total = Decimal("0.00")
        for p in self.payments:
            total += Decimal(p.amount)
        return total.quantize(Decimal("0.01"))

    @property
    def balance(self) -> Decimal:
        return (Decimal(self.gross_total) - self.paid_total).quantize(Decimal("0.01"))

class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    description = Column(Text, nullable=False)
    qty = Column(Numeric(12, 2), nullable=False, default=1)
    unit_price = Column(Numeric(12, 2), nullable=False, default=0)
    vat_rate = Column(Numeric(5, 2), nullable=False, default=21.00)  # 21.00 / 9.00 / 0.00

    line_net = Column(Numeric(12, 2), nullable=False, default=0)
    line_vat = Column(Numeric(12, 2), nullable=False, default=0)
    line_total = Column(Numeric(12, 2), nullable=False, default=0)

    invoice = relationship("Invoice", back_populates="lines")

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)  # can be standalone income
    date = Column(Date, nullable=False, default=date.today)
    amount = Column(Numeric(12, 2), nullable=False)
    method = Column(String(32), nullable=False, default="bank")  # bank, cash, western_union, other
    reference = Column(String(128))
    note = Column(Text)

    invoice = relationship("Invoice", back_populates="payments")

class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, default=date.today)
    vendor = Column(String(160), nullable=False)
    category = Column(String(64), nullable=False)  # Software, DGA Salary, Tax - Wage, Tax - CIT, Travel, Office...
    description = Column(Text, default="")
    currency = Column(String(8), nullable=False, default="EUR")

    # VAT on purchases (input VAT)
    vat_rate = Column(Numeric(5, 2), nullable=False, default=21.00)  # 21/9/0/exempt: store 0 for exempt
    amount_net = Column(Numeric(12, 2), nullable=False, default=0)
    vat_amount = Column(Numeric(12, 2), nullable=False, default=0)
    amount_gross = Column(Numeric(12, 2), nullable=False, default=0)

    receipt_path = Column(String(256))

# Create tables (no destructive changes)
Base.metadata.create_all(engine)

# One-time schema upgrade for rsin (for existing DBs)
def ensure_schema_upgrades():
    insp = inspect(engine)
    if insp.has_table("company_settings"):
        cols = {c['name'] for c in insp.get_columns("company_settings")}
        if "rsin" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE company_settings ADD COLUMN rsin VARCHAR(32) DEFAULT ''"))
ensure_schema_upgrades()

# -------------------------------------------------
# Utilities
# -------------------------------------------------
def dec(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]

def require_csrf(form_token: str):
    tok = session.get("csrf_token")
    if not tok or tok != form_token:
        raise ValueError("CSRF token mismatch")

def get_db():
    return SessionLocal()

@app.teardown_appcontext
def remove_session(_exc=None):
    SessionLocal.remove()

def ensure_company(db):
    row = db.get(CompanySettings, 1)
    if not row:
        row = CompanySettings(id=1)
        db.add(row)
        db.flush()
    # Autofill missing fields with your defaults
    for key, val in DEFAULT_COMPANY.items():
        if not getattr(row, key, None):
            setattr(row, key, val)
    db.commit()
    return row

def next_invoice_no(db, prefix: str) -> str:
    year = date.today().year
    seq = db.execute(
        select(InvoiceSequence).where(InvoiceSequence.year == year, InvoiceSequence.prefix == prefix)
    ).scalar_one_or_none()
    if not seq:
        seq = InvoiceSequence(year=year, prefix=prefix, last_seq=0)
        db.add(seq)
        db.flush()
    seq.last_seq += 1
    db.flush()
    return f"{prefix}-{year}-{seq.last_seq:04d}"

def recalc_invoice(inv: Invoice):
    net = Decimal("0.00")
    vat = Decimal("0.00")
    gross = Decimal("0.00")
    for line in inv.lines:
        ln = dec(line.qty) * dec(line.unit_price)
        lr = dec(line.vat_rate)
        lv = (ln * lr / Decimal("100")).quantize(Decimal("0.01"))
        lt = (ln + lv).quantize(Decimal("0.01"))
        line.line_net = ln; line.line_vat = lv; line.line_total = lt
        net += ln; vat += lv; gross += lt

    # VAT header schemes that zero out VAT on invoice
    if inv.vat_scheme in ("REVERSE_CHARGE_EU", "ZERO_OUTSIDE_EU", "EXEMPT"):
        vat = Decimal("0.00")
        gross = net

    inv.net_total = net.quantize(Decimal("0.01"))
    inv.vat_total = vat.quantize(Decimal("0.01"))
    inv.gross_total = gross.quantize(Decimal("0.01"))

def update_status(inv: Invoice):
    if inv.paid_total <= Decimal("0.00"):
        inv.status = "SENT"
    elif inv.paid_total < Decimal(inv.gross_total):
        inv.status = "PARTIAL"
    else:
        inv.status = "PAID"

def quarter_bounds(d: date):
    q = (d.month - 1)//3 + 1
    start_month = 3*(q-1)+1
    start = date(d.year, start_month, 1)
    end = (start + relativedelta(months=3)) - relativedelta(days=1)
    return start, end

def compliance_warnings(company: CompanySettings, inv: Invoice) -> list[str]:
    """Return list of warnings for Dutch invoice rules."""
    warns = []
    # Supplier details
    if not company.company_name or not company.address or not company.city or not company.postcode:
        warns.append("Company name/address/postcode/city missing in Company Settings.")
    if not company.kvk:
        warns.append("KVK number missing in Company Settings.")
    if not company.rsin:
        warns.append("RSIN missing in Company Settings.")
    if inv.vat_scheme != "EXEMPT":
        if not company.vat_number:
            warns.append("Supplier VAT number missing in Company Settings.")
        elif "[" in (company.vat_number or ""):
            warns.append("Supplier VAT number looks like a placeholder. Replace with your real VAT number.")
    if not company.iban or not company.bic:
        warns.append("IBAN/BIC missing in Company Settings.")

    # Customer details
    if not inv.client_name or not inv.client_address:
        warns.append("Customer name and address required on the invoice.")
    if inv.vat_scheme == "REVERSE_CHARGE_EU" and not inv.client_vat_number:
        warns.append("Customer VAT number required for reverse charge (BTW verlegd).")

    # Invoice fields
    if inv.supply_date is None:
        warns.append("Supply/performance date is required on Dutch invoices.")
    if not inv.lines:
        warns.append("Invoice must contain at least one line with description, qty and unit price.")
    return warns

# -------------------------------------------------
# Routes
# -------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    db = get_db()
    company = ensure_company(db)

    today = date.today()
    q_start, q_end = quarter_bounds(today)

    # YTD
    ytd_income = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.date >= date(today.year, 1, 1))
    ).scalar_one()
    ytd_expenses = db.execute(
        select(func.coalesce(func.sum(Expense.amount_gross), 0)).where(Expense.date >= date(today.year, 1, 1))
    ).scalar_one()

    # VAT (quarter)
    sales_21 = sales_9 = sales_0 = Decimal("0.00")
    vat_out = Decimal("0.00")
    ilines = db.execute(
        select(InvoiceLine, Invoice).join(Invoice).where(
            Invoice.issue_date >= q_start, Invoice.issue_date <= q_end
        )
    ).all()
    for line, inv in ilines:
        net = dec(line.line_net)
        if inv.vat_scheme == "REVERSE_CHARGE_EU":
            # Shown without VAT here (customer accounts for VAT)
            pass
        elif inv.vat_scheme in ("ZERO_OUTSIDE_EU", "EXEMPT"):
            sales_0 += net
        else:
            rate = dec(line.vat_rate)
            if rate == dec("21"): sales_21 += net
            elif rate == dec("9"): sales_9 += net
            else: sales_0 += net
            vat_out += dec(line.line_vat)

    exp_rows = db.execute(select(Expense).where(Expense.date >= q_start, Expense.date <= q_end)).scalars().all()
    vat_in = sum((dec(e.vat_amount) for e in exp_rows), Decimal("0.00"))
    vat_due = (vat_out - vat_in).quantize(Decimal("0.01"))

    # Lists
    recent_invoices = db.execute(select(Invoice).order_by(Invoice.issue_date.desc()).limit(6)).scalars().all()
    unpaid_invoices = db.execute(
        select(Invoice).where(Invoice.status.in_(("SENT","PARTIAL"))).order_by(Invoice.due_date.asc()).limit(6)
    ).scalars().all()
    recent_expenses = db.execute(select(Expense).order_by(Expense.date.desc()).limit(8)).scalars().all()
    recent_payments = db.execute(select(Payment).order_by(Payment.date.desc()).limit(8)).scalars().all()

    return render_template(
        "index.html",
        csrf_token=csrf_token(),
        company=company,
        ytd_income=dec(ytd_income),
        ytd_expenses=dec(ytd_expenses),
        q_start=q_start, q_end=q_end,
        sales_21=sales_21.quantize(Decimal("0.01")),
        sales_9=sales_9.quantize(Decimal("0.01")),
        sales_0=sales_0.quantize(Decimal("0.01")),
        vat_out=vat_out.quantize(Decimal("0.01")),
        vat_in=vat_in.quantize(Decimal("0.01")),
        vat_due=vat_due,
        recent_invoices=recent_invoices,
        unpaid_invoices=unpaid_invoices,
        recent_expenses=recent_expenses,
        recent_payments=recent_payments
    )

@app.route("/settings/save", methods=["POST"])
def save_settings():
    try:
        require_csrf(request.form.get("csrf_token", ""))
        db = get_db(); company = ensure_company(db)
        company.company_name = request.form.get("company_name","").strip()
        company.address = request.form.get("address","").strip()
        company.city = request.form.get("city","").strip()
        company.postcode = request.form.get("postcode","").strip()
        company.country = request.form.get("country","Netherlands").strip()
        company.kvk = request.form.get("kvk","").strip()
        company.rsin = request.form.get("rsin","").strip()
        company.vat_number = request.form.get("vat_number","").strip()
        company.iban = request.form.get("iban","").strip()
        company.bic = request.form.get("bic","").strip()
        company.invoice_prefix = request.form.get("invoice_prefix","INV").strip() or "INV"
        db.commit()
        flash("Company settings saved.", "success")
    except Exception as e:
        get_db().rollback()
        flash(f"Settings error: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/invoice/create", methods=["POST"])
def create_invoice():
    db = get_db()
    try:
        require_csrf(request.form.get("csrf_token",""))
        company = ensure_company(db)
        invoice_no = next_invoice_no(db, company.invoice_prefix)

        issue_date = datetime.strptime(request.form["issue_date"], "%Y-%m-%d").date()
        supply_date = datetime.strptime(request.form["supply_date"], "%Y-%m-%d").date()
        due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()
        currency = request.form.get("currency","EUR").strip().upper()
        vat_scheme = request.form.get("vat_scheme","STANDARD")
        client_name = request.form["client_name"].strip()
        client_address = request.form.get("client_address","").strip()
        client_vat_number = request.form.get("client_vat_number","").strip()
        notes = request.form.get("notes","").strip()

        inv = Invoice(
            invoice_no=invoice_no, issue_date=issue_date, supply_date=supply_date, due_date=due_date,
            currency=currency, vat_scheme=vat_scheme,
            client_name=client_name, client_address=client_address, client_vat_number=client_vat_number,
            notes=notes, status="SENT"
        )
        db.add(inv); db.flush()

        # Lines
        descs = request.form.getlist("line_desc")
        qtys = request.form.getlist("line_qty")
        prices = request.form.getlist("line_price")
        vats = request.form.getlist("line_vat")
        for i in range(len(descs)):
            d = (descs[i] or "").strip()
            if not d:
                continue
            q = dec(qtys[i] or "0")
            up = dec(prices[i] or "0")
            vr = dec(vats[i] or "0")
            line = InvoiceLine(invoice_id=inv.id, description=d, qty=q, unit_price=up, vat_rate=vr)
            db.add(line)
        db.flush()
        recalc_invoice(inv); update_status(inv)

        # Compliance warnings
        for w in compliance_warnings(company, inv):
            flash("Invoice warning: " + w, "warning")

        db.commit()
        flash(f"Invoice {inv.invoice_no} created.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Create invoice error: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/invoice/<int:invoice_id>/pay", methods=["POST"])
def pay_invoice(invoice_id):
    db = get_db()
    try:
        require_csrf(request.form.get("csrf_token",""))
        inv = db.get(Invoice, invoice_id)
        if not inv:
            flash("Invoice not found.", "warning"); return redirect(url_for("index"))
        amount = dec(request.form["amount"])
        pay_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        method = request.form.get("method","bank")
        reference = request.form.get("reference","").strip()
        note = request.form.get("note","").strip()
        p = Payment(invoice_id=invoice_id, date=pay_date, amount=amount, method=method, reference=reference, note=note)
        db.add(p); db.flush()
        update_status(inv); db.commit()
        flash(f"Payment €{amount} recorded for {inv.invoice_no}.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Payment error: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/income/add", methods=["POST"])
def add_income():
    db = get_db()
    try:
        require_csrf(request.form.get("csrf_token",""))
        amount = dec(request.form["amount"])
        pay_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        method = request.form.get("method","bank")
        reference = request.form.get("reference","").strip()
        note = request.form.get("note","").strip()
        p = Payment(invoice_id=None, date=pay_date, amount=amount, method=method, reference=reference, note=note)
        db.add(p); db.commit()
        flash(f"Income €{amount} saved.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Income error: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/expense/add", methods=["POST"])
def add_expense():
    db = get_db()
    try:
        require_csrf(request.form.get("csrf_token",""))
        exp_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        vendor = request.form["vendor"].strip()
        category = request.form.get("category","General").strip()
        description = request.form.get("description","").strip()
        currency = request.form.get("currency","EUR").strip().upper()
        amount_gross = dec(request.form["amount_gross"])
        vat_rate = dec(request.form.get("vat_rate","21"))

        # Compute net and VAT
        if vat_rate <= Decimal("0"):
            amount_net = amount_gross
            vat_amount = Decimal("0.00")
        else:
            amount_net = (amount_gross / (Decimal("1.00") + vat_rate/Decimal("100"))).quantize(Decimal("0.01"))
            vat_amount = (amount_gross - amount_net).quantize(Decimal("0.01"))

        receipt_path = None
        file = request.files.get("receipt")
        if file and file.filename:
            fname = secure_filename(file.filename)
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            base, ext = os.path.splitext(fname)
            fname = f"{base}_{ts}{ext}".replace(" ", "_")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], fname))
            receipt_path = fname

        e = Expense(
            date=exp_date, vendor=vendor, category=category, description=description, currency=currency,
            vat_rate=vat_rate, amount_net=amount_net, vat_amount=vat_amount, amount_gross=amount_gross,
            receipt_path=receipt_path
        )
        db.add(e); db.commit()
        flash("Expense saved.", "success")
    except Exception as e:
        db.rollback()
        flash(f"Expense error: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)

if __name__ == "__main__":
    app.run(debug=True)
