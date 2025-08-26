import os
import secrets
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_from_directory, session
)
from werkzeug.utils import secure_filename

from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Numeric, ForeignKey,
    Text, func, select
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, scoped_session
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

# -------------------------
# Config
# -------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-" + secrets.token_hex(16))

app = Flask(__name__, static_folder=None, template_folder="templates")
app.config.update(SECRET_KEY=SECRET_KEY, UPLOAD_FOLDER=UPLOAD_FOLDER, MAX_CONTENT_LENGTH=25 * 1024 * 1024)

# -------------------------
# DB setup (SQLAlchemy Core)
# -------------------------
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True))
Base = declarative_base()

# -------------------------
# Models
# -------------------------
class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    invoice_no = Column(String(32), unique=True, nullable=False)
    client_name = Column(String(128), nullable=False)
    issue_date = Column(Date, nullable=False, default=date.today)
    due_date = Column(Date, nullable=False)
    currency = Column(String(8), nullable=False, default="EUR")
    amount = Column(Numeric(12, 2), nullable=False)  # total amount (gross)
    status = Column(String(16), nullable=False, default="DRAFT")  # DRAFT, SENT, PARTIAL, PAID
    notes = Column(Text)

    payments = relationship("Payment", back_populates="invoice", cascade="all, delete-orphan")

    @property
    def paid_total(self) -> Decimal:
        total = Decimal("0.00")
        for p in self.payments:
            total += Decimal(p.amount)
        return total.quantize(Decimal("0.01"))

    @property
    def balance(self) -> Decimal:
        return (Decimal(self.amount) - self.paid_total).quantize(Decimal("0.01"))

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)  # nullable -> standalone income
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
    vendor = Column(String(128), nullable=False)
    category = Column(String(64), nullable=False)  # e.g., Software, DGA Salary, Tax - CIT, Travel, etc.
    description = Column(Text)
    currency = Column(String(8), nullable=False, default="EUR")
    amount = Column(Numeric(12, 2), nullable=False)
    receipt_path = Column(String(256))  # filename under uploads/

Base.metadata.create_all(engine)

# -------------------------
# Helpers
# -------------------------
def get_db():
    return SessionLocal()

@app.teardown_appcontext
def remove_session(_exc=None):
    SessionLocal.remove()

def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]

def require_csrf(form_token: str):
    tok = session.get("csrf_token")
    if not tok or tok != form_token:
        raise ValueError("CSRF token mismatch")

def dec(value: str | float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def start_of_month(d: date) -> date:
    return d.replace(day=1)

def start_of_year(d: date) -> date:
    return d.replace(month=1, day=1)

def next_invoice_no(db) -> str:
    y = date.today().year
    # Find last invoice this year
    like = f"INV-{y}-%"
    last = db.execute(
        select(Invoice.invoice_no).where(Invoice.invoice_no.like(like)).order_by(Invoice.invoice_no.desc())
    ).scalars().first()
    seq = 1
    if last:
        try:
            seq = int(last.split("-")[-1]) + 1
        except Exception:
            seq = 1
    return f"INV-{y}-{seq:04d}"

def update_invoice_status(inv: Invoice):
    if inv.paid_total <= Decimal("0.00"):
        inv.status = "SENT" if inv.amount > 0 else "DRAFT"
    elif inv.paid_total < Decimal(inv.amount):
        inv.status = "PARTIAL"
    else:
        inv.status = "PAID"

# -------------------------
# Routes
# -------------------------
@app.route("/")
def index():
    db = get_db()
    today = date.today()
    som = start_of_month(today)
    soy = start_of_year(today)

    # Totals
    mtd_income = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.date >= som)
    ).scalar_one()
    mtd_exp = db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.date >= som)
    ).scalar_one()
    ytd_income = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.date >= soy)
    ).scalar_one()
    ytd_exp = db.execute(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.date >= soy)
    ).scalar_one()

    # Lists
    recent_invoices = db.execute(
        select(Invoice).order_by(Invoice.issue_date.desc()).limit(6)
    ).scalars().all()
    unpaid_invoices = db.execute(
        select(Invoice).where(Invoice.status.in_(("SENT", "PARTIAL"))).order_by(Invoice.due_date.asc()).limit(6)
    ).scalars().all()
    recent_expenses = db.execute(
        select(Expense).order_by(Expense.date.desc()).limit(8)
    ).scalars().all()
    recent_payments = db.execute(
        select(Payment).order_by(Payment.date.desc()).limit(8)
    ).scalars().all()

    return render_template(
        "index.html",
        csrf_token=csrf_token(),
        mtd_income=dec(mtd_income), mtd_expenses=dec(mtd_exp),
        ytd_income=dec(ytd_income), ytd_expenses=dec(ytd_exp),
        recent_invoices=recent_invoices,
        unpaid_invoices=unpaid_invoices,
        recent_expenses=recent_expenses,
        recent_payments=recent_payments,
        next_invoice_guess=next_invoice_no(db)
    )

@app.route("/invoice/create", methods=["POST"])
def create_invoice():
    try:
        require_csrf(request.form.get("csrf_token", ""))
        db = get_db()
        invoice_no = request.form.get("invoice_no") or next_invoice_no(db)
        client_name = request.form["client_name"].strip()
        issue_date = datetime.strptime(request.form["issue_date"], "%Y-%m-%d").date()
        due_date = datetime.strptime(request.form["due_date"], "%Y-%m-%d").date()
        amount = dec(request.form["amount"])
        currency = request.form.get("currency", "EUR").strip().upper()
        notes = request.form.get("notes", "").strip()

        inv = Invoice(
            invoice_no=invoice_no,
            client_name=client_name,
            issue_date=issue_date,
            due_date=due_date,
            amount=amount,
            currency=currency,
            status="SENT",
            notes=notes
        )
        db.add(inv)
        db.commit()
        flash(f"Invoice {inv.invoice_no} created.", "success")
    except Exception as e:
        get_db().rollback()
        flash(f"Error creating invoice: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/invoice/<int:invoice_id>/pay", methods=["POST"])
def pay_invoice(invoice_id):
    try:
        require_csrf(request.form.get("csrf_token", ""))
        db = get_db()
        inv = db.get(Invoice, invoice_id)
        if not inv:
            flash("Invoice not found.", "warning")
            return redirect(url_for("index"))
        amount = dec(request.form["amount"])
        pay_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        method = request.form.get("method", "bank")
        reference = request.form.get("reference", "").strip()
        note = request.form.get("note", "").strip()

        p = Payment(invoice_id=invoice_id, date=pay_date, amount=amount, method=method, reference=reference, note=note)
        db.add(p)
        db.flush()
        update_invoice_status(inv)
        db.commit()
        flash(f"Payment €{amount} recorded for {inv.invoice_no}.", "success")
    except Exception as e:
        get_db().rollback()
        flash(f"Error recording payment: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/income/add", methods=["POST"])
def add_income():
    """Standalone income not tied to an invoice."""
    try:
        require_csrf(request.form.get("csrf_token", ""))
        db = get_db()
        amount = dec(request.form["amount"])
        pay_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        method = request.form.get("method", "bank")
        reference = request.form.get("reference", "").strip()
        note = request.form.get("note", "").strip()

        p = Payment(invoice_id=None, date=pay_date, amount=amount, method=method, reference=reference, note=note)
        db.add(p)
        db.commit()
        flash(f"Income €{amount} saved.", "success")
    except Exception as e:
        get_db().rollback()
        flash(f"Error adding income: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/expense/add", methods=["POST"])
def add_expense():
    try:
        require_csrf(request.form.get("csrf_token", ""))
        db = get_db()
        exp_date = datetime.strptime(request.form["date"], "%Y-%m-%d").date()
        vendor = request.form["vendor"].strip()
        category = request.form.get("category", "General").strip()
        description = request.form.get("description", "").strip()
        amount = dec(request.form["amount"])
        currency = request.form.get("currency", "EUR").strip().upper()

        receipt_path = None
        file = request.files.get("receipt")
        if file and file.filename:
            fname = secure_filename(file.filename)
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            base, ext = os.path.splitext(fname)
            fname = f"{base}_{ts}{ext}".replace(" ", "_")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], fname))
            receipt_path = fname

        exp = Expense(
            date=exp_date, vendor=vendor, category=category,
            description=description, currency=currency, amount=amount,
            receipt_path=receipt_path
        )
        db.add(exp)
        db.commit()
        flash("Expense saved.", "success")
    except Exception as e:
        get_db().rollback()
        flash(f"Error saving expense: {e}", "danger")
    return redirect(url_for("index"))

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)
load_dotenv()  # loads .env if present (local dev); safe in production

if __name__ == "__main__":
    app.run(debug=True)
