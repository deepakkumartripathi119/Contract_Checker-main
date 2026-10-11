import sys
import subprocess

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import simpleSplit
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "reportlab"])
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import simpleSplit

def create_pdf(filename, title, content):
    c = canvas.Canvas(filename, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, height - 72, title)
    
    c.setFont("Helvetica", 12)
    y = height - 100
    
    for section in content:
        lines = simpleSplit(section, "Helvetica", 12, width - 144)
        for line in lines:
            c.drawString(72, y, line)
            y -= 15
            if y < 72:
                c.showPage()
                c.setFont("Helvetica", 12)
                y = height - 72
        y -= 15 # extra space between sections
        
    c.save()

policy_content = [
    "Nexify Solutions Internal Procurement Policy",
    "",
    "1. Payment Terms",
    "Nexify Solutions shall pay all valid and undisputed vendor invoices within 45 days of receipt (Net 45). Early payment discounts are encouraged.",
    "",
    "2. Indemnification",
    "Vendors must indemnify, defend, and hold harmless Nexify Solutions against all third-party claims arising from the vendor's negligence, willful misconduct, or breach of contract.",
    "",
    "3. Dispute Resolution",
    "All disputes arising out of vendor contracts shall be resolved by binding arbitration in New York, NY, under the rules of the American Arbitration Association.",
    "",
    "4. Insurance Requirements",
    "Vendors must maintain commercial general liability insurance with a minimum coverage limit of $2,000,000 per occurrence."
]

contract_content = [
    "Vendor Services Agreement",
    "",
    "This Agreement is between Acme Corp ('Vendor') and Nexify Solutions.",
    "",
    "1. Payment (Edge Case: Benefits Nexify -> Should be Compliant)",
    "Nexify Solutions agrees to pay all undisputed invoices within 90 days of receipt. Furthermore, Nexify Solutions is entitled to a 5% discount if payment is made within 15 days.",
    "",
    "2. Insurance (Edge Case: Harms Nexify -> Should be Non-compliant)",
    "Vendor shall maintain general liability insurance with a minimum coverage limit of $500,000 per occurrence during the term of this Agreement.",
    "",
    "3. Governing Law and Dispute Resolution (Edge Case: Direct Conflict/Harms -> Non-compliant)",
    "In the event of any dispute, the parties agree to resolve the matter exclusively through litigation in the state and federal courts located in Tokyo, Japan.",
    "",
    "4. Publicity (Edge Case: No Policy Coverage -> Compliant by default)",
    "Vendor may use Nexify Solutions' name and logo in its marketing materials and customer lists with prior written consent.",
    "",
    "5. Indemnification (Edge Case: Exact Match -> Compliant)",
    "The Vendor agrees to indemnify and hold harmless Nexify Solutions from any third-party claims resulting from the Vendor's gross negligence or willful misconduct."
]

create_pdf("Nexify_Company_Policy.pdf", "Internal Policy Document", policy_content)
create_pdf("Vendor_Contract_Sample.pdf", "Vendor Contract", contract_content)
print("PDFs generated successfully.")
