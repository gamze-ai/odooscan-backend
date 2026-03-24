"""
main.py — OdooScan Backend API v2.2
"""

import base64
import re
import json
import xmlrpc.client
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from contact_matcher import ContactMatcher, serialize_candidates

def _convert_date(date_str):
    """GG.AA.YYYY formatını YYYY-MM-DD'ye çevirir"""
    if not date_str: return False
    try:
        parts = date_str.replace('/', '.').replace('-', '.').split('.')
        if len(parts) == 3:
            if len(parts[2]) == 4:  # GG.AA.YYYY
                return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
            else:  # YYYY-MM-DD zaten
                return date_str
    except:
        return False

# Türkçe karakter normalize için
TR_MAP = str.maketrans('ığüşöçİĞÜŞÖÇ', 'igusocigusoc')

def normalize_tr(text: str) -> str:
    if not text: return ''
    return text.strip().translate(TR_MAP).lower()

app = FastAPI(title="OdooScan API", version="2.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
claude_client = anthropic.Anthropic()

class OdooConfig(BaseModel):
    url: str
    db: str
    username: str
    api_key: str

class ExtractRequest(BaseModel):
    file_base64: str
    mime_type: str
    document_type: str

class CheckContactRequest(BaseModel):
    odoo_config: OdooConfig
    name: str = ''
    company_name: str = ''
    email: str = ''
    phone: str = ''

class SendToOdooRequest(BaseModel):
    odoo_config: OdooConfig
    scan_type: str
    form_fields: dict = {}
    card_fields: dict = {}
    manual_fields: dict = {}
    selected_contact_id: Optional[int] = None       # Kartvizit kişisi
    selected_company_id: Optional[int] = None
    selected_gorusulen_1_id: Optional[int] = None   # Görüşülen 1 seçimi
    selected_gorusulen_2_id: Optional[int] = None   # Görüşülen 2 seçimi

class OdooOptionsRequest(BaseModel):
    odoo_config: OdooConfig

def get_odoo(cfg: OdooConfig):
    common = xmlrpc.client.ServerProxy(f'{cfg.url.rstrip("/")}/xmlrpc/2/common')
    uid = common.authenticate(cfg.db, cfg.username, cfg.api_key, {})
    if not uid:
        raise HTTPException(status_code=401, detail='Odoo kimlik doğrulama başarısız')
    models = xmlrpc.client.ServerProxy(f'{cfg.url.rstrip("/")}/xmlrpc/2/object')
    return models, uid

def odoo_call(models, cfg, uid, model, method, args, kwargs=None):
    return models.execute_kw(cfg.db, uid, cfg.api_key, model, method, args, kwargs or {})

FORM_PROMPT = """Bu bir FUAR GÖRÜŞME RAPORU formudur.
Formdan bilgileri çıkar ve SADECE JSON döndür. Emin olmadığın alanlara boş string koy.

{
  "fuar_adi": "",
  "sirket": "",
  "tarih": "GG.AA.YYYY formatında",
  "gorusulen_1": "",
  "gorusulen_1_tel": "",
  "gorusulen_1_mail": "",
  "gorusulen_2": "",
  "gorusulen_2_tel": "",
  "gorusulen_2_mail": "",
  "gorusme_yapan_1": "",
  "gorusme_yapan_2": "",
  "gorusme_yapan_3": "",
  "notlar": "",
  "aksiyon_plan": "ZİYARET veya TANITIM veya FİYAT TEKLİFİ veya ARAMA veya CRM",
  "oncelik": "AZ veya ORTA veya ÇOK"
}"""

CARD_PROMPT = """Bu görüntüde bir veya birden fazla KARTVİZİT olabilir.
Tüm kartvizitleri tek tek çıkar ve SADECE JSON döndür. Emin olmadığın alanlara boş string koy.

{
  "cards": [
    {
      "name": "",
      "company": "",
      "function": "",
      "phone": "",
      "mobile": "",
      "email": "",
      "website": "",
      "street": "",
      "city": "ilçe",
      "state": "şehir",
      "zip": "",
      "country": ""
    }
  ]
}"""

BOTH_PROMPT = """Bu görüntüde hem FUAR GÖRÜŞME RAPORU formu hem de bir veya birden fazla KARTVİZİT var.
Tüm bilgileri çıkar ve SADECE JSON döndür.

ÖNEMLİ KURALLAR:
- Görüntüde kaç kartvizit varsa hepsini "cards" listesine ekle
- Formda "Görüşülen 1. Kişi" veya "Görüşülen 2. Kişi" alanları BOŞSA, kartvizitteki kişi adını oraya yaz
- Formda bu alanlar dolu ise kartvizit bilgilerini "cards" listesine ekle, form alanlarına dokunma

{
  "form": {
    "fuar_adi": "", "sirket": "", "tarih": "",
    "gorusulen_1": "", "gorusulen_1_tel": "", "gorusulen_1_mail": "",
    "gorusulen_2": "", "gorusulen_2_tel": "", "gorusulen_2_mail": "",
    "gorusme_yapan_1": "", "gorusme_yapan_2": "", "gorusme_yapan_3": "",
    "notlar": "", "aksiyon_plan": "", "oncelik": ""
  },
  "cards": [
    {
      "name": "", "company": "", "function": "",
      "phone": "", "mobile": "", "email": "", "website": "",
      "street": "", "city": "", "state": "", "zip": "", "country": ""
    }
  ]
}"""

CARD_MULTI_PROMPT = """Bu görüntüde bir veya birden fazla KARTVİZİT var.
Tüm kartvizitleri tek tek çıkar ve SADECE JSON döndür.

{
  "cards": [
    {
      "name": "", "company": "", "function": "",
      "phone": "", "mobile": "", "email": "", "website": "",
      "street": "", "city": "", "state": "", "zip": "", "country": ""
    }
  ]
}"""

@app.post('/api/extract')
async def extract(req: ExtractRequest):
    if req.mime_type == 'application/pdf':
        image_b64 = _pdf_to_image(req.file_base64)
        mime = 'image/png'
    else:
        image_b64 = req.file_base64
        mime = req.mime_type

    prompt = {'form': FORM_PROMPT, 'businessCard': CARD_PROMPT, 'both': BOTH_PROMPT}.get(req.document_type, FORM_PROMPT)

    msg = claude_client.messages.create(
        model='claude-opus-4-5', max_tokens=1500,
        messages=[{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': image_b64}},
            {'type': 'text', 'text': prompt},
        ]}],
    )

    raw = re.sub(r'^```json\s*', '', msg.content[0].text.strip())
    raw = re.sub(r'```$', '', raw).strip()

    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=500, detail=f'Parse hatası: {raw[:300]}')

    if req.document_type == 'both':
        form = data.get('form', {})
        cards = data.get('cards', [])
        card_fields = _merge_cards_to_form(form, cards)
        return {'form_fields': form, 'card_fields': card_fields, 'extra_cards': cards[2:] if len(cards) > 2 else []}
    elif req.document_type == 'businessCard':
        cards = data.get('cards', [data])
        card_fields = cards[0] if cards else {}
        return {'card_fields': card_fields, 'extra_cards': cards[1:], 'form_fields': {}}
    else:
        return {'form_fields': data, 'card_fields': {}}

def _merge_cards_to_form(form: dict, cards: list) -> dict:
    """
    Kartvizit bilgilerini form görüşülen alanlarına aktar.
    Formda gorusulen_1 boşsa ilk kartviziti oraya, gorusulen_2 boşsa ikinci kartviziti oraya yaz.
    """
    remaining = list(cards)

    if not form.get('gorusulen_1') and remaining:
        c = remaining.pop(0)
        form['gorusulen_1'] = c.get('name', '')
        form['gorusulen_1_tel'] = c.get('phone') or c.get('mobile', '')
        form['gorusulen_1_mail'] = c.get('email', '')

    if not form.get('gorusulen_2') and remaining:
        c = remaining.pop(0)
        form['gorusulen_2'] = c.get('name', '')
        form['gorusulen_2_tel'] = c.get('phone') or c.get('mobile', '')
        form['gorusulen_2_mail'] = c.get('email', '')

    # Kalan kartvizitlerden ilkini card_fields olarak döndür (kontak oluşturma için)
    card_fields = remaining[0] if remaining else (cards[0] if cards else {})
    return card_fields

def _pdf_to_image(pdf_b64):
    try:
        import fitz
        doc = fitz.open(stream=base64.b64decode(pdf_b64), filetype='pdf')
        pix = doc[0].get_pixmap(dpi=200)
        return base64.b64encode(pix.tobytes('png')).decode()
    except ImportError:
        return pdf_b64

@app.post('/api/check-contact')
async def check_contact(req: CheckContactRequest):
    try:
        matcher = ContactMatcher(req.odoo_config.url, req.odoo_config.db, req.odoo_config.username, req.odoo_config.api_key)
        matcher._stored_api_key = req.odoo_config.api_key
        contacts = matcher.find_contact_candidates(req.name, req.company_name, req.email, req.phone)
        companies = matcher.find_company_candidates(req.company_name) if req.company_name else []
        return {
            'contact_candidates': serialize_candidates(contacts),
            'company_candidates': serialize_candidates(companies),
            'has_exact_contact': any(c.match_type == 'exact' for c in contacts),
            'has_exact_company': any(c.match_type == 'exact' for c in companies),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post('/api/send-to-odoo')
async def send_to_odoo(req: SendToOdooRequest):
    models, uid = get_odoo(req.odoo_config)
    cfg = req.odoo_config
    ff, cf, mf = req.form_fields, req.card_fields, req.manual_fields
    result = {}

    # ── A. Kontak ID belirle ─────────────────────────────────────────────────
    contact_id = req.selected_contact_id

    # ── B. Şirket belirle ────────────────────────────────────────────────────
    company_name = cf.get('company') or ff.get('sirket') or ''
    company_id = req.selected_company_id

    if not req.selected_contact_id and not company_id and company_name:
        ex = odoo_call(models, cfg, uid, 'res.partner', 'search_read',
            [[['is_company', '=', True], ['name', '=ilike', company_name]]],
            {'fields': ['id', 'name'], 'limit': 1})
        if ex:
            company_id = ex[0]['id']
            result['company_existed'] = True
        else:
            company_id = odoo_call(models, cfg, uid, 'res.partner', 'create',
                [{'name': company_name, 'is_company': True}])
            result['company_created'] = True
            result['company_id'] = company_id

    # ── C. Yeni kontak oluştur (sadece seçilmediyse) ─────────────────────────
    if not contact_id:
        if cf and cf.get('name'):
            vals = {k: v for k, v in {
                'is_company': False,
                'name': cf.get('name', ''),
                'function': cf.get('function', ''),
                'phone': cf.get('phone', ''),
                'mobile': cf.get('mobile', ''),
                'email': cf.get('email', ''),
                'website': cf.get('website', ''),
                'street': cf.get('street', ''),
                'city': cf.get('city', ''),
                'zip': cf.get('zip', ''),
            }.items() if v}

            if company_id:
                vals['parent_id'] = company_id

            if cf.get('state'):
                s = odoo_call(models, cfg, uid, 'res.country.state', 'search',
                    [[['name', 'ilike', cf['state']]]], {'limit': 1})
                if s: vals['state_id'] = s[0]

            if cf.get('country'):
                c = odoo_call(models, cfg, uid, 'res.country', 'search',
                    [[['name', 'ilike', cf['country']]]], {'limit': 1})
                if c: vals['country_id'] = c[0]

            for f in ['user_id', 'property_payment_term_id',
                      'property_supplier_payment_term_id', 'sale_currency_rate_type_id']:
                if mf.get(f): vals[f] = mf[f]

            contact_id = odoo_call(models, cfg, uid, 'res.partner', 'create', [vals])
            result['contact_created'] = True
            result['contact_id'] = contact_id
    else:
        result['contact_existed'] = True
        result['contact_id'] = contact_id

        if not company_id:
            existing = odoo_call(models, cfg, uid, 'res.partner', 'read',
                [[contact_id]], {'fields': ['parent_id']})
            if existing and existing[0].get('parent_id'):
                company_id = existing[0]['parent_id'][0]
                result['company_from_contact'] = True

    # ── D. Görüşülen kişiler ─────────────────────────────────────────────────
    def find_or_create(name, phone='', email='', parent=None):
        if not name: return None
        ex = odoo_call(models, cfg, uid, 'res.partner', 'search_read',
            [[['name', '=ilike', name], ['is_company', '=', False]]],
            {'fields': ['id'], 'limit': 1})
        if ex: return ex[0]['id']
        v = {'name': name, 'is_company': False}
        if phone: v['phone'] = phone
        if email: v['email'] = email
        if parent: v['parent_id'] = parent
        return odoo_call(models, cfg, uid, 'res.partner', 'create', [v])

    # Görüşülen 1 — frontend'den seçim geldiyse kullan, yoksa bul/oluştur
    if req.selected_gorusulen_1_id:
        g1 = req.selected_gorusulen_1_id
    else:
        g1 = find_or_create(ff.get('gorusulen_1',''), ff.get('gorusulen_1_tel',''), ff.get('gorusulen_1_mail',''), company_id)

    # Görüşülen 2 — frontend'den seçim geldiyse kullan, yoksa bul/oluştur
    if req.selected_gorusulen_2_id:
        g2 = req.selected_gorusulen_2_id
    else:
        g2 = find_or_create(ff.get('gorusulen_2',''), ff.get('gorusulen_2_tel',''), ff.get('gorusulen_2_mail',''), company_id)

    # ── E. Görüşme yapanlar — büyük/küçük harf + Türkçe karakter duyarsız ──
    def find_user(name):
        if not name: return None
        norm_target = normalize_tr(name)
        # Tüm aktif dahili kullanıcıları çek, Python tarafında karşılaştır
        users = odoo_call(models, cfg, uid, 'res.users', 'search_read',
            [[['active', '=', True], ['share', '=', False]]],
            {'fields': ['id', 'name'], 'limit': 500})
        # Önce tam eşleşme dene
        for u in users:
            if normalize_tr(u['name']) == norm_target:
                return u['id']
        # Tam eşleşme yoksa kısmi eşleşme dene
        for u in users:
            u_norm = normalize_tr(u['name'])
            if norm_target in u_norm or u_norm in norm_target:
                return u['id']
        return None

    u1 = find_user(ff.get('gorusme_yapan_1',''))
    u2 = find_user(ff.get('gorusme_yapan_2',''))
    u3 = find_user(ff.get('gorusme_yapan_3',''))

    # ── F. Öncelik → puan ───────────────────────────────────────────────────
    puan = {'AZ': '1', 'ORTA': '2', 'ÇOK': '3', 'COK': '3'}.get(
        ff.get('oncelik','').upper().strip(), '1')

    # ── G. Aksiyon ──────────────────────────────────────────────────────────
    aksiyon_map = {
        "ZİYARET": "Ziyaret", "ZIYARET": "Ziyaret", "ZiYARET": "Ziyaret",
        "TANITIM": "Tanıtım", "TANİTIM": "Tanıtım",
        "FİYAT TEKLİFİ": "Fiyat Teklifi", "FIYAT TEKLIFI": "Fiyat Teklifi",
        "ARAMA": "Arama", "CRM": "CRM",
    }
    aksiyon = aksiyon_map.get(ff.get('aksiyon_plan','').upper().strip(), '')

    # ── H. Ziyaret kaydı ────────────────────────────────────────────────────
    # Görüşülen kişiler: form + kartvizit kaynaklı
    gorusulen_1_id = g1 or contact_id
    gorusulen_2_id = g2

    # Birden fazla kartvizit varsa extra_cards'dan görüşülen oluştur
    extra_cards = req.form_fields.get('_extra_cards', [])
    for extra in extra_cards[:1]:  # Şimdilik en fazla 1 ekstra
        if not gorusulen_2_id and extra.get('name'):
            gorusulen_2_id = find_or_create(
                extra.get('name',''), extra.get('phone') or extra.get('mobile',''), extra.get('email',''), company_id)

    visit_vals = {k: v for k, v in {
        'x_name': ff.get('fuar_adi', ''),
        'x_studio_field_YnEYp': company_id,
        'x_studio_field_SnOyH': _convert_date(ff.get('tarih', '')),
        'x_studio_grlen': gorusulen_1_id,
        'x_studio_grlen_2_kii': gorusulen_2_id,
        'x_studio_aksiyon_sorumlusu': u1,
        'x_studio_ziyaret_eden_2': u2,
        'x_studio_ziyaret_eden_3': u3,
        'x_studio_notlar': ff.get('notlar', ''),
        'x_studio_aksiyon_plan': aksiyon,
        'x_studio_grme': puan,
        'x_studio_field_TmyaU': 'Fuar',
        'x_studio_ziyaret_toplant_gerekleti_mi_1': 'Evet',
    }.items() if v}

    try:
        visit_id = odoo_call(models, cfg, uid, 'x_ziyaretler', 'create', [visit_vals])
        result['visit_id'] = visit_id
        result['visit_created'] = True
        result['odoo_url'] = f"{cfg.url}/web#id={visit_id}&model=x_ziyaretler&view_type=form"
    except Exception as e:
        result['visit_error'] = str(e)

    result['success'] = True
    return result

@app.post('/api/odoo-options')
async def get_odoo_options(req: OdooOptionsRequest):
    """Frontend dropdown'ları için Odoo'dan seçenek listelerini dinamik çeker."""
    try:
        models, uid = get_odoo(req.odoo_config)
        cfg = req.odoo_config

        # Satış Temsilcisi — dahili aktif kullanıcılar
        users = odoo_call(models, cfg, uid, 'res.users', 'search_read',
            [[['active', '=', True], ['share', '=', False]]],
            {'fields': ['id', 'name'], 'limit': 500, 'order': 'name asc'})
        users_list = [{'id': u['id'], 'name': u['name']} for u in users]

        # Ödeme Koşulları (Müşteri + Tedarikçi için aynı liste)
        payment_terms = odoo_call(models, cfg, uid, 'account.payment.term', 'search_read',
            [[['active', '=', True]]],
            {'fields': ['id', 'name'], 'limit': 200, 'order': 'name asc'})
        pt_list = [{'id': p['id'], 'name': p['name']} for p in payment_terms]

        # Satış Kur Türü — res.currency.rate.type
        currency_rate_types = []
        try:
            crt = odoo_call(models, cfg, uid, 'res.currency.rate.type', 'search_read',
                [[]],
                {'fields': ['id', 'name'], 'limit': 100, 'order': 'name asc'})
            currency_rate_types = [{'id': c['id'], 'name': c['name']} for c in crt]
        except Exception:
            pass  # Model bazı Odoo kurulumlarında olmayabilir

        return {
            'users': users_list,
            'payment_terms': pt_list,
            'currency_rate_types': currency_rate_types,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post('/api/test-connection')
async def test_connection(cfg: OdooConfig):
    """Ayarlar ekranındaki bağlantı test butonu için."""
    try:
        common = xmlrpc.client.ServerProxy(f'{cfg.url.rstrip("/")}/xmlrpc/2/common')
        uid = common.authenticate(cfg.db, cfg.username, cfg.api_key, {})
        return {'success': bool(uid), 'uid': uid} if uid else {'success': False, 'message': 'Kimlik doğrulama başarısız'}
    except Exception as e:
        return {'success': False, 'message': str(e)}

@app.post('/api/test-odoo')
async def test_odoo(cfg: OdooConfig):
    """Eski endpoint — geriye dönük uyumluluk için."""
    return await test_connection(cfg)

@app.get('/health')
def health():
    return {'status': 'ok'}
