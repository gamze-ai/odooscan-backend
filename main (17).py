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
    create_new_gorusulen_1: bool = False             # Görüşülen 1 yeni kontak oluşturulsun
    create_new_gorusulen_2: bool = False             # Görüşülen 2 yeni kontak oluşturulsun
    selected_gorusulen_3_id: Optional[int] = None   # Görüşülen 3 seçimi
    create_new_gorusulen_3: bool = False             # Görüşülen 3 yeni kontak oluşturulsun
    gorusulen_1_data: Optional[dict] = None          # Yeni kontak için kullanıcının girdiği veriler
    gorusulen_2_data: Optional[dict] = None
    gorusulen_3_data: Optional[dict] = None
    extra_cards: list = []                           # Ekstra kartvizit kişileri
    create_new_company: bool = False                 # Yeni şirket oluşturulsun
    company_data: Optional[dict] = None              # Yeni şirket için kullanıcı verileri

class LoginRequest(BaseModel):
    odoo_url: str
    db: str
    email: str
    password: str

@app.post('/api/login')
async def login(req: LoginRequest):
    try:
        common = xmlrpc.client.ServerProxy(f'{req.odoo_url.rstrip("/")}/xmlrpc/2/common')
        uid = common.authenticate(req.db, req.email, req.password, {})
        if not uid:
            return {'success': False, 'message': 'E-posta veya şifre hatalı'}
        # Kullanıcı adını ve grup bilgisini al
        models = xmlrpc.client.ServerProxy(f'{req.odoo_url.rstrip("/")}/xmlrpc/2/object')
        users = models.execute_kw(req.db, uid, req.password, 'res.users', 'read',
            [[uid]], {'fields': ['name', 'email', 'groups_id']})
        user = users[0] if users else {}

        # Yönetim/Ayarlar grubunu kontrol et (base.group_system)
        is_admin = False
        try:
            group_ids = models.execute_kw(req.db, uid, req.password, 'res.groups', 'search',
                [[['category_id.name', '=', 'Administration'], ['name', 'in', ['Settings', 'Access Rights']]]],
                {})
            # base.group_system XML ID'sini de dene
            system_group = models.execute_kw(req.db, uid, req.password, 'ir.model.data', 'search_read',
                [[['module', '=', 'base'], ['name', '=', 'group_system']]],
                {'fields': ['res_id'], 'limit': 1})
            if system_group:
                group_ids.append(system_group[0]['res_id'])
            user_group_ids = user.get('groups_id', [])
            is_admin = any(gid in user_group_ids for gid in group_ids)
        except Exception:
            is_admin = False

        return {
            'success': True,
            'uid': uid,
            'name': user.get('name', ''),
            'email': user.get('email', req.email),
            'api_key': req.password,
            'is_admin': is_admin,
        }
    except Exception as e:
        return {'success': False, 'message': str(e)}

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
- Her fiziksel kartviziti YALNIZCA BİR KEZ "cards" listesine ekle — aynı kişiyi asla tekrarlama
- Bir kartvizit birden fazla logo/marka içerse bile TEK kartvizit olarak say
- Aynı isim veya şirket birden fazla görünse bile "cards" listesine sadece bir kez ekle
- Formda görüşülen alanları BOŞSA: kartvizitteki kişiyi SADECE EN UYGUN BİR ALANA yaz, aynı kişiyi birden fazla görüşülen alanına YAZMA
- Formda görüşülen alanlar dolu ise kartvizit bilgilerini "cards" listesine ekle, form alanlarına dokunma

{
  "form": {
    "fuar_adi": "", "sirket": "", "tarih": "",
    "gorusulen_1": "", "gorusulen_1_tel": "", "gorusulen_1_mail": "",
    "gorusulen_2": "", "gorusulen_2_tel": "", "gorusulen_2_mail": "",
    "gorusulen_3": "", "gorusulen_3_tel": "", "gorusulen_3_mail": "",
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
        # Aynı isimli kartları tekilleştir (AI bazen aynı kartviziti iki kez okuyabilir)
        seen_names = set()
        unique_cards = []
        for c in cards:
            name_key = (c.get('name') or '').strip().lower()
            if name_key and name_key in seen_names:
                continue
            seen_names.add(name_key)
            unique_cards.append(c)
        card_fields, remaining_cards = _merge_cards_to_form(form, unique_cards)

        # Aynı kişi birden fazla görüşülen alanına yazılmışsa temizle
        seen_gorusulen = set()
        for slot in ['gorusulen_1', 'gorusulen_2', 'gorusulen_3']:
            name = (form.get(slot) or '').strip().lower()
            if not name:
                continue
            if name in seen_gorusulen:
                form[slot] = ''
                form[slot + '_tel'] = ''
                form[slot + '_mail'] = ''
            else:
                seen_gorusulen.add(name)

        return {'form_fields': form, 'card_fields': card_fields, 'extra_cards': remaining_cards}
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

    if not form.get('gorusulen_3') and remaining:
        c = remaining.pop(0)
        form['gorusulen_3'] = c.get('name', '')
        form['gorusulen_3_tel'] = c.get('phone') or c.get('mobile', '')
        form['gorusulen_3_mail'] = c.get('email', '')

    # remaining'den card_fields al; taşınan kartın bilgilerini de sakla
    if remaining:
        card_fields = remaining[0]
        extra = remaining[1:]
    elif cards:
        # Tüm kartlar görüşülen alanlara taşındı
        # card_fields olarak döndür ama "merged" işaretini koy
        card_fields = {**cards[0], '_merged_to_form': True}
        extra = []
    else:
        card_fields = {}
        extra = []
    return card_fields, extra

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

    # Şirket — kullanıcı seçtiyse kullan, yeni oluşturulsun dediyse oluştur
    if not company_id and req.selected_company_id:
        company_id = req.selected_company_id
        result['company_existed'] = True
    elif not company_id and req.create_new_company and company_name:
        cd = req.company_data or {}
        company_vals = {k: v for k, v in {
            'name': cd.get('name') or company_name,
            'is_company': True,
            'phone': cd.get('phone', ''),
            'email': cd.get('email', ''),
            'website': cd.get('website', ''),
            'vat': cd.get('vat', ''),
            'street': cd.get('street', ''),
        }.items() if v and v is not True}
        company_vals['is_company'] = True
        if cd.get('country_id'):
            company_vals['country_id'] = cd['country_id']
        elif cd.get('country'):
            c = odoo_call(models, cfg, uid, 'res.country', 'search',
                [[['name', 'ilike', cd['country']]]], {'limit': 1})
            if c: company_vals['country_id'] = c[0]
        if cd.get('state_id'):
            company_vals['state_id'] = cd['state_id']
        company_id = odoo_call(models, cfg, uid, 'res.partner', 'create', [company_vals])
        result['company_created'] = True
        result['company_id'] = company_id

    # ── C. Yeni kontak oluştur (sadece seçilmediyse) ─────────────────────────

    if not contact_id:
        # Kartvizit kişisi zaten g1/g2/g3 olarak işlendiyse tekrar oluşturma
        card_norm = normalize_tr(cf.get('name', ''))
        already_used = any(
            card_norm and normalize_tr(ff.get(k, '')) and (
                card_norm in normalize_tr(ff.get(k, '')) or
                normalize_tr(ff.get(k, '')) in card_norm
            )
            for k in ['gorusulen_1', 'gorusulen_2', 'gorusulen_3']
        )
        if already_used:
            # Var olan ID'yi bul ve contact_id olarak ata
            contact_id = next((x for x in [g1, g2, g3] if x), None)
        elif cf and cf.get('name'):
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
    def find_or_create(name, phone='', email='', parent=None, extra_data=None, manual_fields=None, force_create=False):
        if not name: return None
        # force_create=True ise aramayı atla, direkt oluştur
        if not force_create:
            ex = odoo_call(models, cfg, uid, 'res.partner', 'search_read',
                [[['name', '=ilike', name], ['is_company', '=', False]]],
                {'fields': ['id'], 'limit': 1})
            if ex: return ex[0]['id']
        v = {'name': name, 'is_company': False}
        # extra_data'dan gelen değerler form değerlerini override eder
        eff_phone = (extra_data or {}).get('phone') or phone
        eff_email = (extra_data or {}).get('email') or email
        if eff_phone: v['phone'] = eff_phone
        if eff_email: v['email'] = eff_email
        if parent: v['parent_id'] = parent
        # Ekstra kişi verisi (modal'dan geldi)
        if extra_data:
            for fld in ['function','mobile','website','street','city','zip']:
                if extra_data.get(fld): v[fld] = extra_data[fld]
            # Dropdown'dan gelen ID'ler
            if extra_data.get('state_id'): v['state_id'] = extra_data['state_id']
            elif extra_data.get('state'):
                s = odoo_call(models, cfg, uid, 'res.country.state', 'search',
                    [[['name', 'ilike', extra_data['state']]]], {'limit': 1})
                if s: v['state_id'] = s[0]
            if extra_data.get('country_id'): v['country_id'] = extra_data['country_id']
            elif extra_data.get('country'):
                c = odoo_call(models, cfg, uid, 'res.country', 'search',
                    [[['name', 'ilike', extra_data['country']]]], {'limit': 1})
                if c: v['country_id'] = c[0]
        # Odoo zorunlu alanları
        if manual_fields:
            for f in ['user_id','property_payment_term_id','property_supplier_payment_term_id','sale_currency_rate_type_id']:
                if manual_fields.get(f): v[f] = manual_fields[f]
        return odoo_call(models, cfg, uid, 'res.partner', 'create', [v])

    # Görüşülen 1
    if req.selected_gorusulen_1_id:
        g1 = req.selected_gorusulen_1_id                          # Mevcut seçildi
    elif req.create_new_gorusulen_1 and ff.get('gorusulen_1'):
        g1 = find_or_create(ff['gorusulen_1'],
            ff.get('gorusulen_1_tel',''), ff.get('gorusulen_1_mail',''), company_id,
            extra_data=req.gorusulen_1_data, manual_fields=mf, force_create=True)
    elif ff.get('gorusulen_1'):
        ex = odoo_call(models, cfg, uid, 'res.partner', 'search_read',  # Sadece ara
            [[['name', '=ilike', ff['gorusulen_1']], ['is_company', '=', False]]],
            {'fields': ['id'], 'limit': 1})
        g1 = ex[0]['id'] if ex else None
    else:
        g1 = None

    # Görüşülen 2
    if req.selected_gorusulen_2_id:
        g2 = req.selected_gorusulen_2_id
    elif req.create_new_gorusulen_2 and ff.get('gorusulen_2'):
        g2 = find_or_create(ff['gorusulen_2'],
            ff.get('gorusulen_2_tel',''), ff.get('gorusulen_2_mail',''), company_id,
            extra_data=req.gorusulen_2_data, manual_fields=mf, force_create=True)
    elif ff.get('gorusulen_2'):
        ex = odoo_call(models, cfg, uid, 'res.partner', 'search_read',
            [[['name', '=ilike', ff['gorusulen_2']], ['is_company', '=', False]]],
            {'fields': ['id'], 'limit': 1})
        g2 = ex[0]['id'] if ex else None
    else:
        g2 = None

    # Görüşülen 3
    if req.selected_gorusulen_3_id:
        g3 = req.selected_gorusulen_3_id
    elif req.create_new_gorusulen_3 and ff.get('gorusulen_3'):
        g3 = find_or_create(ff['gorusulen_3'],
            ff.get('gorusulen_3_tel',''), ff.get('gorusulen_3_mail',''), company_id,
            extra_data=req.gorusulen_3_data, manual_fields=mf, force_create=True)
    elif ff.get('gorusulen_3'):
        ex = odoo_call(models, cfg, uid, 'res.partner', 'search_read',
            [[['name', '=ilike', ff['gorusulen_3']], ['is_company', '=', False]]],
            {'fields': ['id'], 'limit': 1})
        g3 = ex[0]['id'] if ex else None
    else:
        g3 = None

    # Kullanılan kontak ID'lerini takip et — aynı kişi birden fazla alana yazılmasın
    used_contact_ids = set(x for x in [g1, g2, g3] if x)

    # ── E. Görüşme yapanlar — büyük/küçük harf + Türkçe karakter duyarsız ──
    def find_user(name):
        if not name: return None
        norm_target = normalize_tr(name)
        # Tüm aktif dahili kullanıcıları çek, Python tarafında karşılaştır
        users = odoo_call(models, cfg, uid, 'res.users', 'search_read',
            [[['active', '=', True]]],
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

    # Ekstra kartvizit kişilerini işle
    extra_contact_ids = []
    for ec in req.extra_cards:
        ec_data = ec.get('card_data', {})
        ec_contact_id = ec.get('contact_id')
        ec_is_new = ec.get('is_new', False)
        ec_manual = ec.get('manual_fields', {})

        if ec_contact_id:
            extra_contact_ids.append(ec_contact_id)
        elif ec_is_new and ec_data.get('name'):
            # Yeni kontak oluştur
            ec_vals = {k: v for k, v in {
                'is_company': False,
                'name': ec_data.get('name', ''),
                'function': ec_data.get('function', ''),
                'phone': ec_data.get('phone', ''),
                'mobile': ec_data.get('mobile', ''),
                'email': ec_data.get('email', ''),
            }.items() if v}
            if company_id: ec_vals['parent_id'] = company_id
            for f in ['user_id', 'property_payment_term_id',
                      'property_supplier_payment_term_id', 'sale_currency_rate_type_id']:
                if ec_manual.get(f): ec_vals[f] = ec_manual[f]
            ec_id = odoo_call(models, cfg, uid, 'res.partner', 'create', [ec_vals])
            extra_contact_ids.append(ec_id)
        elif ec_data.get('name'):
            # Sadece ara
            ex = odoo_call(models, cfg, uid, 'res.partner', 'search_read',
                [[['name', '=ilike', ec_data['name']], ['is_company', '=', False]]],
                {'fields': ['id'], 'limit': 1})
            if ex: extra_contact_ids.append(ex[0]['id'])

    # Görüşülen 2 boşsa ilk ekstra kartvizitten doldur
    if not gorusulen_2_id and extra_contact_ids:
        gorusulen_2_id = extra_contact_ids.pop(0)

    # Görüşülen 3 boşsa sonraki ekstra kartvizitten doldur
    if not g3 and extra_contact_ids:
        g3 = extra_contact_ids.pop(0)

    visit_vals = {k: v for k, v in {
        'x_name': ff.get('fuar_adi', ''),
        'x_studio_field_YnEYp': company_id,
        'x_studio_field_SnOyH': _convert_date(ff.get('tarih', '')),
        'x_studio_grlen': gorusulen_1_id,
        'x_studio_grlen_2_kii': gorusulen_2_id,
        'x_studio_grlen_3_kii': g3,
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
        err_str = str(e)
        # Odoo UserError mesajını temizle
        import re as _re
        match = _re.search(r"'([^']{10,})'", err_str)
        friendly = match.group(1) if match else err_str
        result['visit_error'] = friendly

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

        # Ülkeler (res.country)
        countries = odoo_call(models, cfg, uid, 'res.country', 'search_read',
            [[]],
            {'fields': ['id', 'name'], 'limit': 300, 'order': 'name asc'})
        countries_list = [{'id': c['id'], 'name': c['name']} for c in countries]

        return {
            'users': users_list,
            'payment_terms': pt_list,
            'currency_rate_types': currency_rate_types,
            'countries': countries_list,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class StatesByCountryRequest(BaseModel):
    odoo_config: OdooConfig
    country_id: int

@app.post('/api/states-by-country')
async def get_states_by_country(req: StatesByCountryRequest):
    try:
        models, uid = get_odoo(req.odoo_config)
        cfg = req.odoo_config
        states = odoo_call(models, cfg, uid, 'res.country.state', 'search_read',
            [[['country_id', '=', req.country_id]]],
            {'fields': ['id', 'name'], 'limit': 500, 'order': 'name asc'})
        return {'states': [{'id': s['id'], 'name': s['name']} for s in states]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class UpdateVisitRequest(BaseModel):
    odoo_config: OdooConfig
    visit_id: int
    form_fields: dict = {}
    card_fields: dict = {}

@app.post('/api/update-visit')
async def update_visit(req: UpdateVisitRequest):
    try:
        models, uid = get_odoo(req.odoo_config)
        cfg = req.odoo_config
        ff = req.form_fields

        aksiyon_map = {
            "ZİYARET": "Ziyaret", "ZIYARET": "Ziyaret",
            "TANITIM": "Tanıtım", "TANİTIM": "Tanıtım",
            "FİYAT TEKLİFİ": "Fiyat Teklifi", "FIYAT TEKLIFI": "Fiyat Teklifi",
            "ARAMA": "Arama", "CRM": "CRM",
        }
        aksiyon = aksiyon_map.get(ff.get('aksiyon_plan','').upper().strip(), '')
        puan = {'AZ': '1', 'ORTA': '2', 'ÇOK': '3', 'COK': '3'}.get(
            ff.get('oncelik','').upper().strip(), '')

        update_vals = {k: v for k, v in {
            'x_name': ff.get('fuar_adi', ''),
            'x_studio_field_SnOyH': _convert_date(ff.get('tarih', '')),
            'x_studio_notlar': ff.get('notlar', ''),
            'x_studio_aksiyon_plan': aksiyon,
            'x_studio_grme': puan,
        }.items() if v}

        if update_vals:
            odoo_call(models, cfg, uid, 'x_ziyaretler', 'write', [[req.visit_id], update_vals])

        return {'success': True, 'visit_id': req.visit_id}
    except Exception as e:
        return {'success': False, 'error': str(e)}

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
