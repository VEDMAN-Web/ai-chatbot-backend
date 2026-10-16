import os
import io
import csv
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from abc import ABC, abstractmethod
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.db.models import Property, PropertySpec, Lead
from app.core.config import settings

logger = logging.getLogger(__name__)

# 1. Abstract Storage Interface
class StorageProvider(ABC):
    @abstractmethod
    def save_file(self, filename: str, content: bytes, content_type: str = "application/pdf", **kwargs) -> str:
        """Saves file bytes and returns a retrieval URL or local path."""
        pass


class LocalStorageProvider(StorageProvider):
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or settings.REPORT_STORAGE_DIR
        os.makedirs(self.base_dir, exist_ok=True)

    def cleanup_old_reports(self, max_files: int = 5, max_age_hours: int = 24) -> int:
        """
        Automatically prunes old generated report files to prevent disk clutter.
        Keeps at most `max_files` latest reports, and removes any files older than `max_age_hours`.
        Returns number of deleted files.
        """
        if not os.path.exists(self.base_dir):
            return 0
        now = datetime.now().timestamp()
        deleted_count = 0
        try:
            files = []
            for fname in os.listdir(self.base_dir):
                if fname.startswith("tajlandia_report_") and (fname.endswith(".xlsx") or fname.endswith(".pdf") or fname.endswith(".docx") or fname.endswith(".csv")):
                    full_path = os.path.join(self.base_dir, fname)
                    if os.path.isfile(full_path):
                        mtime = os.path.getmtime(full_path)
                        files.append((full_path, mtime))

            # Sort newest first
            files.sort(key=lambda x: x[1], reverse=True)

            for idx, (path, mtime) in enumerate(files):
                age_hours = (now - mtime) / 3600.0
                if idx >= max_files or age_hours > max_age_hours:
                    try:
                        os.remove(path)
                        deleted_count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return deleted_count

    def save_file(self, filename: str, content: bytes, content_type: str = "application/pdf", **kwargs) -> str:
        file_path = os.path.join(self.base_dir, filename)
        with open(file_path, "wb") as f:
            f.write(content)
        # Automatically prune old report files
        self.cleanup_old_reports(max_files=5, max_age_hours=24)
        if "property" in filename.lower() or "brochure" in filename.lower():
            return f"/api/v1/chat/brochure/file/{filename}"
        return f"/api/v1/admin/reports/{filename}"


class S3StorageProvider(StorageProvider):
    def __init__(
        self,
        bucket_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        region_name: str = "us-east-1",
        profile_name: Optional[str] = None,
        folder: str = "brochures"
    ):
        self.endpoint_url = endpoint_url or os.getenv("HF_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "https://s3.hf.co/itisha249"
        self.bucket_name = bucket_name or os.getenv("HF_S3_BUCKET") or os.getenv("AWS_S3_BUCKET") or "tajlandia"
        self.region_name = region_name or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
        self.profile_name = profile_name or os.getenv("AWS_PROFILE") or "hf"
        self.folder = folder

    def _get_s3_client(self):
        import boto3
        from botocore.config import Config

        boto_config = Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4"
        )

        access_key = os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("HF_ACCESS_KEY")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY") or os.getenv("HF_SECRET_KEY")

        if access_key and secret_key:
            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=self.region_name
            )
        else:
            try:
                session = boto3.Session(profile_name=self.profile_name, region_name=self.region_name)
            except Exception:
                session = boto3.Session(region_name=self.region_name)

        return session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region_name,
            config=boto_config
        )

    def save_file(self, filename: str, content: bytes, content_type: str = "application/pdf") -> str:
        key = f"{self.folder}/{filename}" if self.folder else filename
        try:
            s3 = self._get_s3_client()
            s3.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type
            )
            try:
                # Generate a 7-day presigned URL for direct, secure access
                presigned_url = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket_name, "Key": key},
                    ExpiresIn=604800  # 7 days in seconds
                )
                return presigned_url
            except Exception as pe:
                logger.warning(f"Could not generate presigned URL, using path-style URL: {pe}")
                return f"{self.endpoint_url.rstrip('/')}/{self.bucket_name}/{key}"
        except Exception as e:
            logger.error(f"S3 upload failed for {filename}: {e}. Falling back to local storage.", exc_info=True)
            fallback = LocalStorageProvider()
            return fallback.save_file(filename, content)


def get_storage_provider() -> StorageProvider:
    # Use S3 storage if configured in environment, otherwise local fallback
    has_s3_env = bool(
        os.getenv("HF_S3_ENDPOINT_URL") or
        os.getenv("AWS_S3_BUCKET") or
        os.getenv("HF_S3_BUCKET") or
        os.getenv("AWS_ACCESS_KEY_ID") or
        os.getenv("HF_ACCESS_KEY") or
        os.path.exists(os.path.expanduser("~/.aws/credentials")) or
        os.path.exists(os.path.expanduser("~/.aws/config"))
    )
    if has_s3_env:
        return S3StorageProvider()
    return LocalStorageProvider()


# Default storage provider instance
default_storage_provider = get_storage_provider()


# 2. Report Builders
def _build_excel_report(properties_data: List[Dict[str, Any]], title: str) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Property Report"

    # Header styling
    ws.append([title])
    ws.merge_cells("A1:H1")
    title_cell = ws["A1"]
    title_cell.font = Font(size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")

    headers = [
        "Property ID", "Property Name", "Location", "Price Display", 
        "Bedrooms", "Bathrooms", "Status", "Developer"
    ]
    ws.append(headers)
    for col_num, _ in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_num)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")

    for p in properties_data:
        ws.append([
            p.get("property_id", ""),
            p.get("property_name", ""),
            p.get("location", ""),
            p.get("price_display", ""),
            p.get("bedrooms", ""),
            p.get("bathrooms", ""),
            p.get("status", ""),
            p.get("developer_name", "")
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_pdf_report(properties_data: List[Dict[str, Any]], title: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    # Title
    story.append(Paragraph(f"<b>{title}</b>", styles["Title"]))
    story.append(Spacer(1, 12))

    # Metadata
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Generated: {date_str} | Total Properties: {len(properties_data)}", styles["Normal"]))
    story.append(Spacer(1, 16))

    # Table data
    table_data = [["ID", "Property Name", "Location", "Price", "Status"]]
    for p in properties_data:
        table_data.append([
            p.get("property_id", ""),
            p.get("property_name", "")[:28],
            p.get("location", "")[:20],
            p.get("price_display", "")[:20],
            p.get("status", "")[:18]
        ])

    table = Table(table_data, colWidths=[80, 160, 110, 100, 90])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
    ]))
    story.append(table)

    doc.build(story)
    return buf.getvalue()


def _build_docx_report(properties_data: List[Dict[str, Any]], title: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    doc.add_paragraph(f"Total Properties Listed: {len(properties_data)}")

    table = doc.add_table(rows=1, cols=5)
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = "Property ID"
    hdr_cells[1].text = "Name"
    hdr_cells[2].text = "Location"
    hdr_cells[3].text = "Price"
    hdr_cells[4].text = "Status"

    for p in properties_data:
        row_cells = table.add_row().cells
        row_cells[0].text = str(p.get("property_id", ""))
        row_cells[1].text = str(p.get("property_name", ""))
        row_cells[2].text = str(p.get("location", ""))
        row_cells[3].text = str(p.get("price_display", ""))
        row_cells[4].text = str(p.get("status", ""))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_csv_report(properties_data: List[Dict[str, Any]]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["property_id", "property_name", "location", "price_display", "bedrooms", "bathrooms", "status", "developer_name"])
    for p in properties_data:
        writer.writerow([
            p.get("property_id", ""),
            p.get("property_name", ""),
            p.get("location", ""),
            p.get("price_display", ""),
            p.get("bedrooms", ""),
            p.get("bathrooms", ""),
            p.get("status", ""),
            p.get("developer_name", "")
        ])
    return output.getvalue().encode("utf-8")


# 2b. Lead Report Builders
def _build_lead_pdf_report(leads_data: List[Dict[str, Any]], title: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'LeadTitle',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0F172A")
    )
    meta_style = ParagraphStyle(
        'LeadMeta',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#64748B")
    )
    cell_bold = ParagraphStyle(
        'LeadCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.white
    )
    cell_regular = ParagraphStyle(
        'LeadCellReg',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#334155")
    )

    story.append(Paragraph(f"<b>{title}</b>", title_style))
    story.append(Spacer(1, 6))

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph(f"Generated: {date_str} | Verified Buyer Leads: {len(leads_data)} | Confidential Agency Internal", meta_style))
    story.append(Spacer(1, 14))

    for idx, lead in enumerate(leads_data):
        score_val = lead.get('lead_score', 'N/A')
        status_val = str(lead.get('status', 'new')).upper()
        header_text = f"LEAD #{idx+1}: {lead.get('contact_name', 'Client')} &nbsp;|&nbsp; Status: {status_val} &nbsp;|&nbsp; Score: {score_val}/100"

        table_rows = [
            [Paragraph(f"<b>{header_text}</b>", cell_bold), ""],
            [Paragraph(f"<b>Email:</b> {lead.get('contact_email', 'N/A')}", cell_regular),
             Paragraph(f"<b>Phone:</b> {lead.get('contact_phone', 'N/A')}", cell_regular)],
            [Paragraph(f"<b>Preferred Location:</b> {lead.get('preferred_location', 'Thailand')}", cell_regular),
             Paragraph(f"<b>Target Budget:</b> {lead.get('budget', 'Flexible')}", cell_regular)],
            [Paragraph(f"<b>Client Notes:</b> {lead.get('notes', 'Web inquiry')}", cell_regular), ""]
        ]
        signals = lead.get('buying_signals', [])
        if signals:
            sig_text = " • ".join(str(s) for s in signals)
            table_rows.append([Paragraph(f"<b>Buying Signals:</b> {sig_text}", cell_regular), ""])

        t = Table(table_rows, colWidths=[270, 270])
        t_style = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('SPAN', (0, 0), (1, 0)),
            ('SPAN', (0, 3), (1, 3)),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#CBD5E1")),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]
        if signals:
            t_style.append(('SPAN', (0, 4), (1, 4)))
        t.setStyle(TableStyle(t_style))
        story.append(t)
        story.append(Spacer(1, 14))

    doc.build(story)
    return buf.getvalue()


def _build_lead_excel_report(leads_data: List[Dict[str, Any]], title: str) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Buyer Leads"

    ws.append([title])
    ws.merge_cells("A1:H1")
    title_cell = ws["A1"]
    title_cell.font = Font(size=14, bold=True, color="FFFFFF")
    title_cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")

    headers = ["Lead ID", "Contact Name", "Email", "Phone", "Location", "Budget", "Lead Score", "Status", "Notes"]
    ws.append(headers)
    for col_num, _ in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_num)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")

    for l in leads_data:
        ws.append([
            l.get("lead_id", ""),
            l.get("contact_name", ""),
            l.get("contact_email", ""),
            l.get("contact_phone", ""),
            l.get("preferred_location", ""),
            l.get("budget", ""),
            l.get("lead_score", ""),
            l.get("status", ""),
            l.get("notes", "")
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_lead_docx_report(leads_data: List[Dict[str, Any]], title: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    doc.add_paragraph(f"Total Qualified Buyer Leads: {len(leads_data)}")

    table = doc.add_table(rows=1, cols=5)
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = "Lead ID"
    hdr_cells[1].text = "Contact Name"
    hdr_cells[2].text = "Location / Budget"
    hdr_cells[3].text = "Score / Status"
    hdr_cells[4].text = "Notes"

    for l in leads_data:
        row_cells = table.add_row().cells
        row_cells[0].text = str(l.get("lead_id", ""))
        row_cells[1].text = f"{l.get('contact_name', '')} ({l.get('contact_email', '')})"
        row_cells[2].text = f"{l.get('preferred_location', '')} / {l.get('budget', '')}"
        row_cells[3].text = f"{l.get('lead_score', '')} pts ({l.get('status', '')})"
        row_cells[4].text = str(l.get("notes", ""))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_lead_csv_report(leads_data: List[Dict[str, Any]]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["lead_id", "contact_name", "email", "phone", "preferred_location", "budget", "lead_score", "status", "notes"])
    for l in leads_data:
        writer.writerow([
            l.get("lead_id", ""),
            l.get("contact_name", ""),
            l.get("contact_email", ""),
            l.get("contact_phone", ""),
            l.get("preferred_location", ""),
            l.get("budget", ""),
            l.get("lead_score", ""),
            l.get("status", ""),
            l.get("notes", "")
        ])
    return output.getvalue().encode("utf-8")


# 3. Main Generation Entrypoint
def generate_report(
    db: Session,
    format_type: str,
    property_ids: Optional[List[str]] = None,
    lead_ids: Optional[List[str]] = None,
    report_type: Optional[str] = None,
    title: Optional[str] = None,
    storage: Optional[StorageProvider] = None,
    websites: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Builds property intelligence or buyer lead intelligence reports in PDF, XLSX, DOCX, or CSV.
    Stores file via abstract StorageProvider and returns download URL.
    """
    storage = storage or default_storage_provider
    fmt = format_type.strip().lower()
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    is_lead_report = (
        report_type == "leads"
        or (lead_ids is not None and len(lead_ids) > 0)
        or (title and "lead" in title.lower())
    )

    if is_lead_report:
        report_title = title or "Tajlandia AI - Buyer Lead Intelligence Dossier"
        query = db.query(Lead)
        leads = []
        if lead_ids:
            clean_lids = [str(lid).strip() for lid in lead_ids if lid]
            leads = query.filter(
                (Lead.contact_name.in_(clean_lids))
                | (Lead.id.in_(clean_lids))
            ).all()

        if not leads and not lead_ids:
            leads = query.order_by(Lead.lead_score.desc(), Lead.created_at.desc()).limit(10).all()

        leads_data = []
        # Never invent sample leads — empty DB must yield an empty report
        for l in leads:
            notes_val = "Web chat inquiry"
            if l.buying_signals and isinstance(l.buying_signals, list):
                for sig in l.buying_signals:
                    if isinstance(sig, str) and sig.startswith("Note: "):
                        notes_val = sig.replace("Note: ", "")
                        break
                else:
                    if len(l.buying_signals) > 0 and isinstance(l.buying_signals[0], str):
                        notes_val = l.buying_signals[0]

            leads_data.append({
                "lead_id": str(l.id),
                "contact_name": l.contact_name or "Inquiring Visitor",
                "contact_email": l.contact_email or "Not provided",
                "contact_phone": l.contact_phone or "Not provided",
                "preferred_location": l.preferred_location or "Thailand",
                "budget": f"{float(l.budget_max):,} THB" if getattr(l, "budget_max", None) else "Flexible",
                "lead_score": getattr(l, "lead_score", 0.0) or 0.0,
                "status": getattr(l, "status", "new") or "new",
                "notes": notes_val,
                "buying_signals": getattr(l, "buying_signals", []) or []
            })

        filename = f"tajlandia_lead_report_{timestamp_str}.{fmt}"
        if fmt == "xlsx":
            content_bytes = _build_lead_excel_report(leads_data, report_title)
        elif fmt == "pdf":
            content_bytes = _build_lead_pdf_report(leads_data, report_title)
        elif fmt == "docx":
            content_bytes = _build_lead_docx_report(leads_data, report_title)
        elif fmt == "csv":
            content_bytes = _build_lead_csv_report(leads_data)
        else:
            raise ValueError(f"Unsupported report format: {fmt}. Supported formats: pdf, xlsx, docx, csv.")

        file_url = storage.save_file(filename, content_bytes)
        return {
            "format": fmt,
            "filename": filename,
            "file_url": file_url,
            "title": report_title,
            "total_leads": len(leads_data),
            "generated_at": datetime.now(timezone.utc).isoformat()
        }

    # Property Report Generation
    report_title = title or "Tajlandia AI Property Intelligence Inventory Report"
    from app.services.property_search import build_property_query, PropertyFilterParams

    # build_property_query expects a Session (not an existing Query)
    params = PropertyFilterParams(websites=websites or None)
    query = build_property_query(db, params)
    if property_ids:
        clean_ids = [str(pid).strip() for pid in property_ids if pid]
        # Match both exact IDs and case variants used in Firestore/SQL
        upper_ids = [c.upper() for c in clean_ids]
        query = query.filter(
            or_(
                Property.property_id.in_(clean_ids),
                Property.property_id.in_(upper_ids),
            )
        )
    properties = query.all()

    properties_data = []
    for prop in properties:
        loc = f"{prop.district or ''}, {prop.province or ''}".strip(", ")
        properties_data.append({
            "property_id": prop.property_id,
            "property_name": prop.property_name,
            "location": loc or "Thailand",
            "price_display": prop.price_display or "Price on Request",
            "bedrooms": prop.bedrooms_display or str(prop.bedrooms_min or "N/A"),
            "bathrooms": prop.bathrooms or "N/A",
            "status": prop.status or "Available",
            "developer_name": prop.developer_name or "Tajlandia Partner"
        })

    filename = f"tajlandia_report_{timestamp_str}.{fmt}"
    if fmt == "xlsx":
        content_bytes = _build_excel_report(properties_data, report_title)
    elif fmt == "pdf":
        content_bytes = _build_pdf_report(properties_data, report_title)
    elif fmt == "docx":
        content_bytes = _build_docx_report(properties_data, report_title)
    elif fmt == "csv":
        content_bytes = _build_csv_report(properties_data)
    else:
        raise ValueError(f"Unsupported report format: {fmt}. Supported formats: pdf, xlsx, docx, csv.")

    file_url = storage.save_file(filename, content_bytes)

    return {
        "format": fmt,
        "filename": filename,
        "file_url": file_url,
        "title": report_title,
        "total_properties": len(properties_data),
        "generated_at": datetime.now(timezone.utc).isoformat()
    }
