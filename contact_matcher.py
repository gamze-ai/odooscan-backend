"""
contact_matcher.py
Kontak normalizasyon + fuzzy eşleştirme + mükerrer kayıt önleme
v2: Önce Odoo'da direkt arama, sonra fuzzy matching
"""

import re
import xmlrpc.client
from dataclasses import dataclass

try:
    from rapidfuzz import fuzz
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False

COMPANY_SUFFIXES = {
    r'\ba\.ş\.?\b': 'as', r'\banonim şirket(i)?\b': 'as',
    r'\bltd\.?\s*şti\.?\b': 'ltd', r'\blimited şirket(i)?\b': 'ltd',
    r'\ba\.s\.?\b': 'as', r'\bllc\b': 'ltd', r'\binc\.?\b': 'inc',
    r'\bgmbh\b': 'gmbh', r'\bholding\b': 'holding', r'\bgroup\b': 'group',
}

TR_CHAR_MAP = str.maketrans('ığüşöçİĞÜŞÖÇ', 'igusocIGUSOC')


@dataclass
class MatchCandidate:
    odoo_id: int
    odoo_name: str
    odoo_company: str
    score: float
    match_type: str
    display: str


def normalize_name(raw: str) -> str:
    if not raw: return ''
    text = raw.strip().translate(TR_CHAR_MAP).lower()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text.strip()


def normalize_company(raw: str) -> str:
    if not raw: return ''
    text = raw.strip().translate(TR_CHAR_MAP).lower()
    text = re.sub(r'\s+', ' ', text)
    for pattern, replacement in COMPANY_SUFFIXES.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r'[^\w\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def fuzzy_score(a: str, b: str) -> float:
    if not a or not b: return 0.0
    if a == b: return 100.0
    if FUZZY_AVAILABLE:
        return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b))
    longer = max(len(a), len(b))
    if longer == 0: return 100.0
    return (sum(c1 == c2 for c1, c2 in zip(a, b)) / longer) * 100


class ContactMatcher:
    EXACT_THRESHOLD = 95
    HIGH_THRESHOLD = 80
    MEDIUM_THRESHOLD = 40  # Düşürüldü

    def __init__(self, odoo_url: str, db: str, username: str, api_key: str):
        self.url = odoo_url.rstrip('/')
        self.db = db
        self._api_key = api_key
        common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
        self.uid = common.authenticate(self.db, username, api_key, {})
        if not self.uid:
            raise ConnectionError('Odoo kimlik doğrulama başarısız')
        self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')

    def _call(self, model, method, args, kwargs=None):
        return self.models.execute_kw(self.db, self.uid, self._api_key, model, method, args, kwargs or {})

    def find_contact_candidates(self, name: str, company_name: str = '', email: str = '', phone: str = '') -> list:
        candidates = []
        seen_ids = set()
        norm_name = normalize_name(name)
        norm_company = normalize_company(company_name)

        # 1. E-posta ile kesin eşleşme
        if email:
            results = self._call('res.partner', 'search_read',
                [[['email', '=ilike', email.strip()], ['is_company', '=', False]]],
                {'fields': ['id', 'name', 'parent_id', 'email', 'phone'], 'limit': 5})
            for r in results:
                if r['id'] not in seen_ids:
                    seen_ids.add(r['id'])
                    company = r['parent_id'][1] if r.get('parent_id') else ''
                    candidates.append(MatchCandidate(r['id'], r['name'], company, 100.0, 'exact',
                        f"{r['name']} — {company} (e-posta eşleşti)"))

        # 2. Telefon ile eşleşme
        if phone:
            norm_phone = re.sub(r'\D', '', phone)
            for field in ['phone', 'mobile']:
                results = self._call('res.partner', 'search_read',
                    [[['is_company', '=', False], [field, '!=', False]]],
                    {'fields': ['id', 'name', 'parent_id', 'phone', 'mobile'], 'limit': 500})
                for r in results:
                    if r['id'] in seen_ids: continue
                    val = re.sub(r'\D', '', r.get(field) or '')
                    if val and (val == norm_phone or val.endswith(norm_phone[-9:]) or norm_phone.endswith(val[-9:])):
                        company = r['parent_id'][1] if r.get('parent_id') else ''
                        seen_ids.add(r['id'])
                        candidates.append(MatchCandidate(r['id'], r['name'], company, 98.0, 'exact',
                            f"{r['name']} — {company} (telefon eşleşti)"))

        # 3. Odoo'da direkt isim araması (ilike ile) — ÖNEMLİ: kısa isimleri yakalar
        if norm_name:
            # İsmin her kelimesini ayrı ayrı ara
            words = norm_name.split()
            for word in words:
                if len(word) < 2: continue
                results = self._call('res.partner', 'search_read',
                    [[['name', 'ilike', word], ['is_company', '=', False], ['active', '=', True]]],
                    {'fields': ['id', 'name', 'parent_id'], 'limit': 100})
                for r in results:
                    if r['id'] in seen_ids: continue
                    norm_odoo = normalize_name(r['name'])
                    score = fuzzy_score(norm_name, norm_odoo)
                    company = r['parent_id'][1] if r.get('parent_id') else ''
                    # Şirket bonusu
                    if company_name and company:
                        cs = fuzzy_score(norm_company, normalize_company(company))
                        if cs >= 80: score = min(100, score + 5)
                        elif cs < 40: score = max(0, score - 10)
                    if score >= self.MEDIUM_THRESHOLD:
                        seen_ids.add(r['id'])
                        match_type = 'exact' if score >= self.EXACT_THRESHOLD else ('high' if score >= self.HIGH_THRESHOLD else 'medium')
                        candidates.append(MatchCandidate(r['id'], r['name'], company, round(score, 1), match_type,
                            f"{r['name']} — {company} (%{score:.0f})"))

        # 4. Fuzzy matching — tüm kontaklardan (limit 2000)
        if norm_name:
            all_contacts = self._call('res.partner', 'search_read',
                [[['is_company', '=', False], ['active', '=', True]]],
                {'fields': ['id', 'name', 'parent_id'], 'limit': 2000})
            for contact in all_contacts:
                if contact['id'] in seen_ids: continue
                norm_odoo = normalize_name(contact['name'])
                score = fuzzy_score(norm_name, norm_odoo)
                company = contact['parent_id'][1] if contact.get('parent_id') else ''
                if company_name and company:
                    cs = fuzzy_score(norm_company, normalize_company(company))
                    if cs >= 80: score = min(100, score + 5)
                    elif cs < 40: score = max(0, score - 10)
                if score >= self.MEDIUM_THRESHOLD:
                    seen_ids.add(contact['id'])
                    match_type = 'exact' if score >= self.EXACT_THRESHOLD else ('high' if score >= self.HIGH_THRESHOLD else 'medium')
                    candidates.append(MatchCandidate(contact['id'], contact['name'], company, round(score, 1), match_type,
                        f"{contact['name']} — {company} (%{score:.0f})"))

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:10]

    def find_company_candidates(self, company_name: str) -> list:
        norm = normalize_company(company_name)
        candidates = []
        seen_ids = set()

        # Direkt arama
        words = norm.split()
        for word in words:
            if len(word) < 2: continue
            results = self._call('res.partner', 'search_read',
                [[['name', 'ilike', word], ['is_company', '=', True], ['active', '=', True]]],
                {'fields': ['id', 'name'], 'limit': 50})
            for r in results:
                if r['id'] in seen_ids: continue
                score = fuzzy_score(norm, normalize_company(r['name']))
                if score >= self.MEDIUM_THRESHOLD:
                    seen_ids.add(r['id'])
                    match_type = 'exact' if score >= self.EXACT_THRESHOLD else ('high' if score >= self.HIGH_THRESHOLD else 'medium')
                    candidates.append(MatchCandidate(r['id'], r['name'], '', round(score, 1), match_type, f"{r['name']} (%{score:.0f})"))

        # Fuzzy
        companies = self._call('res.partner', 'search_read',
            [[['is_company', '=', True], ['active', '=', True]]],
            {'fields': ['id', 'name'], 'limit': 1000})
        for c in companies:
            if c['id'] in seen_ids: continue
            score = fuzzy_score(norm, normalize_company(c['name']))
            if score >= self.MEDIUM_THRESHOLD:
                seen_ids.add(c['id'])
                match_type = 'exact' if score >= self.EXACT_THRESHOLD else ('high' if score >= self.HIGH_THRESHOLD else 'medium')
                candidates.append(MatchCandidate(c['id'], c['name'], '', round(score, 1), match_type, f"{c['name']} (%{score:.0f})"))

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:5]


def serialize_candidates(candidates: list) -> list:
    return [{'id': c.odoo_id, 'name': c.odoo_name, 'company': c.odoo_company,
             'score': c.score, 'match_type': c.match_type, 'display': c.display}
            for c in candidates]
