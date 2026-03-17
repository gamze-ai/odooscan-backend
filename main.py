"""
main.py — OdooScan Backend API
OCR + AI extraction + Odoo kontak eşleştirme + kayıt oluşturma
"""

import base64
import re
import xmlrpc.client
from io import BytesIO
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from contact_matcher import ContactMatcher, normalize_name, normalize_company, serialize_candidates

app = FastAPI(title="OdooScan API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

claude_client = anthropic.Anthropic()  # ANTHROPIC_API_KEY env var'dan okur


# ─── Pydantic Modeller ────────────────────────────────────────────────────────

class OdooConfig(BaseModel):
    url: str
    db: str
    username: str
    api_key: str


class ExtractRequest(BaseModel):
    file_base64: str
    mime_type: str          # image/jpeg | image/png | application/pdf
    document_type: str      # businessCard | handwrittenForm | pdf
    custom_fields: list[dict] = []   # [{key, label}]


class CheckContactRequest(BaseModel):
    odoo_config: OdooConfig
    name: str
    company_name: str = ''
    email: str = ''
    phone: str = ''


class SendToOdooRequest(BaseModel):
    odoo_config: OdooConfig
    contact_fields: dict
    custom_fields: dict = {}
    custom_module: str = 'x_scanned_document'
    # Kullanıcı mobilde seçim yaptıysa bu ID'ler gelir
    selected_contact_id: Optional[int] = None    # mevcut kontağı kullan
    selected_company_id: Optional[int] = None    # mevcut şirketi kullan
    create_new_contact: bool = False              # yeni kontak oluştur


# ─── Yardımcı: Odoo XML-RPC bağlantısı ───────────────────────────────────────

def get_odoo_models(cfg: OdooConfig):
    common = xmlrpc.client.ServerProxy(f'{cfg.url.rstrip("/")}/xmlrpc/2/common')
    uid = common.authenticate(cfg.db, cfg.username, cfg.api_key, {})
    if not uid:
        raise HTTPException(status_code=401, detail='Odoo kimlik doğrulama başarısız')
    models = xmlrpc.client.ServerProxy(f'{cfg.url.rstrip("/")}/xmlrpc/2/object')
    return models, uid


def odoo_call(models, cfg: OdooConfig, uid: int, model: str, method: str, args, kwargs=None):
    return models.execute_kw(cfg.db, uid, cfg.api_key, model, method, args, kwargs or {})


# ─── 1. OCR + AI Extraction ───────────────────────────────────────────────────

@app.post('/api/extract')
async def extract(req: ExtractRequest):
    """
    Belgeden (kartvizit/form/pdf) bilgileri çıkar.
    Claude Vision API kullanır.
    """
    # PDF → sayfa görüntüsüne çevir
    if req.mime_type == 'application/pdf':
        image_content = _pdf_to_image_base64(req.file_base64)
        mime = 'image/png'
    else:
        image_content = req.file_base64
        mime = req.mime_type

    # Özel alan listesi varsa prompt'a ekle
    custom_fields_prompt = ''
    if req.custom_fields:
        field_list = '\n'.join(f'  - {f["key"]}: {f["label"]}' for f in req.custom_fields)
        custom_fields_prompt = f"""
Ayrıca belgeden şu ÖZEL ALANLARI da çıkar (yoksa boş bırak):
{field_list}
Bu alanları "custom_fields" anahtarı altında ver.
"""

    doc_context = {
        'businessCard': 'Bu bir kartvizit fotoğrafıdır.',
        'handwrittenForm': 'Bu el yazısıyla doldurulmuş bir formdur.',
        'pdf': 'Bu bir PDF form belgesidir.',
    }.get(req.document_type, 'Bu bir belge fotoğrafıdır.')

    prompt = f"""
{doc_context}

Belgeden aşağıdaki bilgileri çıkar ve SADECE JSON formatında döndür.
Emin olmadığın alanlara boş string koy. Tahmin etme.

Döndür:
{{
  "contact_fields": {{
    "name": "kişinin tam adı (tam olarak belgede yazdığı gibi)",
    "company": "şirket adı (tam olarak belgede yazdığı gibi)",
    "job_title": "unvan/pozisyon",
    "phone": "sabit hat",
    "mobile": "cep telefonu",
    "email": "e-posta",
    "website": "web sitesi",
    "street": "adres",
    "city": "şehir",
    "country": "ülke",
    "notes": "belgede diğer önemli notlar"
  }},
  "custom_fields": {{}}
}}
{custom_fields_prompt}
JSON dışında hiçbir şey yazma.
"""

    message = claude_client.messages.create(
        model='claude-opus-4-5',
        max_tokens=1000,
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': mime,
                        'data': image_content,
                    },
                },
                {'type': 'text', 'text': prompt},
            ],
        }],
    )

    raw = message.content[0].text.strip()
    # JSON fence temizle
    raw = re.sub(r'^```json\s*', '', raw)
    raw = re.sub(r'```$', '', raw).strip()

    import json
    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=500, detail=f'AI yanıtı parse edilemedi: {raw[:200]}')

    return data


def _pdf_to_image_base64(pdf_b64: str) -> str:
    """PDF'in ilk sayfasını PNG'ye çevir"""
    try:
        import fitz  # PyMuPDF
        pdf_bytes = base64.b64decode(pdf_b64)
        doc = fitz.open(stream=pdf_bytes, filetype='pdf')
        page = doc[0]
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes('png')
        return base64.b64encode(img_bytes).decode()
    except ImportError:
        # PyMuPDF yoksa pdf_b64'ü direkt dön (bazı modeller PDF kabul eder)
        return pdf_b64


# ─── 2. Kontak Eşleşme Kontrolü ──────────────────────────────────────────────

@app.post('/api/check-contact')
async def check_contact(req: CheckContactRequest):
    """
    Odoo'da kontak fuzzy search yap.
    Mobil uygulama bu endpoint'i çağırır, kullanıcıya aday listesi gösterir.
    """
    try:
        matcher = _build_matcher(req.odoo_config)
        contact_candidates = matcher.find_contact_candidates(
            name=req.name,
            company_name=req.company_name,
            email=req.email,
            phone=req.phone,
        )
        company_candidates = matcher.find_company_candidates(req.company_name) if req.company_name else []

        return {
            'contact_candidates': serialize_candidates(contact_candidates),
            'company_candidates': serialize_candidates(company_candidates),
            'has_exact_contact': any(c.match_type == 'exact' for c in contact_candidates),
            'has_exact_company': any(c.match_type == 'exact' for c in company_candidates),
        }
    except ConnectionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _build_matcher(cfg: OdooConfig) -> ContactMatcher:
    matcher = ContactMatcher(cfg.url, cfg.db, cfg.username, cfg.api_key)
    matcher._stored_api_key = cfg.api_key
    return matcher


# ─── 3. Odoo'ya Kaydet ───────────────────────────────────────────────────────

@app.post('/api/send-to-odoo')
async def send_to_odoo(req: SendToOdooRequest):
    """
    Kontak + şirket + özel modül kaydını Odoo'ya oluştur/güncelle.
    Kullanıcı mobilde zaten bir seçim yaptıysa (selected_contact_id),
    yeni kontak oluşturmaz, mevcut ID'yi kullanır.
    """
    models, uid = get_odoo_models(req.odoo_config)
    cfg = req.odoo_config
    cf = req.contact_fields
    result = {}

    # ── Şirket ID'si bul veya oluştur ──────────────────────────────────────
    company_id = req.selected_company_id
    if not company_id and cf.get('company'):
        # Son bir kontrol (kullanıcı seçmemiş olabilir ama tam eşleşme var)
        existing = odoo_call(models, cfg, uid, 'res.partner', 'search_read',
            [[['is_company', '=', True], ['name', '=ilike', cf['company']]]],
            {'fields': ['id', 'name'], 'limit': 1}
        )
        if existing:
            company_id = existing[0]['id']
            result['company_existed'] = True
        else:
            company_id = odoo_call(models, cfg, uid, 'res.partner', 'create',
                [{'name': cf['company'], 'is_company': True}]
            )
            result['company_created'] = True
            result['company_id'] = company_id

    # ── Kontak ID'si bul veya oluştur ──────────────────────────────────────
    contact_id = req.selected_contact_id

    if not contact_id:
        # Yeni kontak oluştur
        partner_vals = {
            'is_company': False,
            'name': cf.get('name', ''),
            'job_title': cf.get('job_title', ''),
            'phone': cf.get('phone', ''),
            'mobile': cf.get('mobile', ''),
            'email': cf.get('email', ''),
            'website': cf.get('website', ''),
            'street': cf.get('street', ''),
            'city': cf.get('city', ''),
            'comment': cf.get('notes', ''),
        }
        if company_id:
            partner_vals['parent_id'] = company_id
        if cf.get('country'):
            country_ids = odoo_call(models, cfg, uid, 'res.country', 'search',
                [[['name', 'ilike', cf['country']]]], {'limit': 1}
            )
            if country_ids:
                partner_vals['country_id'] = country_ids[0]

        # Boş alanları temizle
        partner_vals = {k: v for k, v in partner_vals.items() if v}

        contact_id = odoo_call(models, cfg, uid, 'res.partner', 'create', [partner_vals])
        result['contact_created'] = True
    else:
        result['contact_existed'] = True

    result['contact_id'] = contact_id

    # ── Özel modüle kayıt oluştur ──────────────────────────────────────────
    module = req.custom_module
    custom_vals = {
        'x_contact_id': contact_id,
        **{k: v for k, v in req.custom_fields.items() if v},
    }

    try:
        record_id = odoo_call(models, cfg, uid, module, 'create', [custom_vals])
        result['record_id'] = record_id
        result['record_created'] = True
    except Exception as e:
        result['record_error'] = str(e)
        result['record_error_hint'] = (
            f'"{module}" modülü bulunamadı veya x_contact_id alanı yok. '
            'Odoo Studio\'dan modül teknik adını ve alanlarını kontrol edin.'
        )

    # Odoo'daki kontak URL'si
    result['odoo_url'] = f"{cfg.url}/web#id={contact_id}&model=res.partner&view_type=form"
    result['success'] = True

    return result


# ─── 4. Bağlantı Testi ───────────────────────────────────────────────────────

@app.post('/api/test-odoo')
async def test_odoo(cfg: OdooConfig):
    try:
        common = xmlrpc.client.ServerProxy(f'{cfg.url.rstrip("/")}/xmlrpc/2/common')
        uid = common.authenticate(cfg.db, cfg.username, cfg.api_key, {})
        if uid:
            return {'success': True, 'uid': uid, 'message': 'Bağlantı başarılı'}
        return {'success': False, 'message': 'Kimlik doğrulama başarısız'}
    except Exception as e:
        return {'success': False, 'message': str(e)}


@app.get('/health')
def health():
    return {'status': 'ok'}
