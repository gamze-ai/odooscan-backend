"""
contact_matcher.py
Kontak normalizasyon + fuzzy eşleştirme + mükerrer kayıt önleme
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional
import xmlrpc.client

try:
    from rapidfuzz import fuzz, process
    FUZZY_AVAILABLE = True
except ImportError:
    # Yedek: basit lowercase karşılaştırma
    FUZZY_AVAILABLE = False

# ─── Şirket eki normalizasyonu ────────────────────────────────────────────────
COMPANY_SUFFIXES = {
    r'\ba\.ş\.?\b': 'as',
    r'\banonim şirket(i)?\b': 'as',
    r'\bltd\.?\s*şti\.?\b': 'ltd',
    r'\blimited şirket(i)?\b': 'ltd',
    r'\ba\.s\.?\b': 'as',          # Azerbaycan vs Türkiye farkı
    r'\bllc\b': 'ltd',
    r'\binc\.?\b': 'inc',
    r'\bgmbh\b': 'gmbh',
    r'\bholding\b': 'holding',
    r'\bgroup\b': 'group',
    r'\bgrp\.?\b': 'group',
}

# Türkçe karakter dönüşüm tablosu
TR_CHAR_MAP = str.maketrans('ığüşöçİĞÜŞÖÇ', 'igusocIGUSOC')


@dataclass
class MatchCandidate:
    """Bir eşleşme adayı"""
    odoo_id: int
    odoo_name: str
    odoo_company: str
    score: float          # 0-100 arası benzerlik skoru
    match_type: str       # 'exact' | 'high' | 'medium'
    display: str          # Kullanıcıya gösterilecek metin


def normalize_name(raw: str) -> str:
    """
    Kişi adını normalize et:
      - GAMZE YILMAZ → gamze yilmaz
      - Türkçe karakter → ASCII
      - Fazla boşluk temizle
    """
    if not raw:
        return ''
    text = raw.strip()
    text = text.translate(TR_CHAR_MAP)
    text = text.lower()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)  # noktalama kaldır
    return text.strip()


def normalize_company(raw: str) -> str:
    """
    Şirket adını normalize et:
      - ABC A.Ş. → abc as
      - ABC Anonim Şirketi → abc as
      - TEKNOLOJI LTD.ŞTI. → teknoloji ltd
    """
    if not raw:
        return ''
    text = raw.strip()
    text = text.translate(TR_CHAR_MAP)
    text = text.lower()
    text = re.sub(r'\s+', ' ', text)

    # Şirket eklerini standartlaştır
    for pattern, replacement in COMPANY_SUFFIXES.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(r'[^\w\s]', '', text)  # noktalama kaldır
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def fuzzy_score(a: str, b: str) -> float:
    """İki normalize edilmiş string arasındaki benzerlik skoru (0-100)"""
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if FUZZY_AVAILABLE:
        # token_sort_ratio: kelime sırasından bağımsız karşılaştırma
        # "Ahmet Yılmaz" vs "Yılmaz Ahmet" → %100
        return max(
            fuzz.token_sort_ratio(a, b),
            fuzz.partial_ratio(a, b),
        )
    else:
        # Basit karakter bazlı benzerlik
        longer = max(len(a), len(b))
        if longer == 0:
            return 100.0
        common = sum(c1 == c2 for c1, c2 in zip(a, b))
        return (common / longer) * 100


class ContactMatcher:
    """
    Odoo'daki mevcut kontakları fuzzy search ile tarar,
    mükerrer kayıt riskini değerlendirir.
    """

    EXACT_THRESHOLD = 95    # Bu puanın üstü: kesin eşleşme
    HIGH_THRESHOLD = 80     # Bu puanın üstü: yüksek ihtimal
    MEDIUM_THRESHOLD = 60   # Bu puanın üstü: orta ihtimal (göster ama sor)

    def __init__(self, odoo_url: str, db: str, username: str, api_key: str):
        self.url = odoo_url.rstrip('/')
        self.db = db
        self.uid = None
        self._connect(username, api_key)

    def _connect(self, username: str, api_key: str):
        common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
        self.uid = common.authenticate(self.db, username, api_key, {})
        if not self.uid:
            raise ConnectionError('Odoo kimlik doğrulama başarısız')
        self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')

    def _odoo_search(self, domain, fields, limit=50):
        return self.models.execute_kw(
            self.db, self.uid, self._api_key_placeholder,
            'res.partner', 'search_read',
            [domain],
            {'fields': fields, 'limit': limit}
        )

    def find_contact_candidates(
        self,
        name: str,
        company_name: str = '',
        email: str = '',
        phone: str = '',
    ) -> list[MatchCandidate]:
        """
        Verilen kişi adı + şirket için Odoo'da aday listesi döndürür.
        Önce e-posta/telefon ile kesin arama, sonra fuzzy.
        """
        candidates: list[MatchCandidate] = []
        seen_ids = set()

        norm_name = normalize_name(name)
        norm_company = normalize_company(company_name)

        # ─── 1. E-posta ile kesin eşleşme ─────────────────────────────────
        if email:
            results = self.models.execute_kw(
                self.db, self.uid, self._api_key,
                'res.partner', 'search_read',
                [[['email', '=ilike', email.strip()], ['is_company', '=', False]]],
                {'fields': ['id', 'name', 'parent_id', 'email', 'phone', 'mobile'], 'limit': 5}
            )
            for r in results:
                if r['id'] not in seen_ids:
                    seen_ids.add(r['id'])
                    company = r['parent_id'][1] if r.get('parent_id') else ''
                    candidates.append(MatchCandidate(
                        odoo_id=r['id'],
                        odoo_name=r['name'],
                        odoo_company=company,
                        score=100.0,
                        match_type='exact',
                        display=f"{r['name']} — {company} (e-posta eşleşti)",
                    ))

        # ─── 2. Telefon ile kesin eşleşme ─────────────────────────────────
        if phone:
            norm_phone = re.sub(r'\D', '', phone)  # sadece rakamlar
            for field in ['phone', 'mobile']:
                results = self.models.execute_kw(
                    self.db, self.uid, self._api_key,
                    'res.partner', 'search_read',
                    [[['is_company', '=', False]]],
                    {'fields': ['id', 'name', 'parent_id', 'phone', 'mobile'], 'limit': 100}
                )
                for r in results:
                    if r['id'] in seen_ids:
                        continue
                    val = re.sub(r'\D', '', r.get(field) or '')
                    if val and (val == norm_phone or val.endswith(norm_phone[-9:]) or norm_phone.endswith(val[-9:])):
                        company = r['parent_id'][1] if r.get('parent_id') else ''
                        seen_ids.add(r['id'])
                        candidates.append(MatchCandidate(
                            odoo_id=r['id'],
                            odoo_name=r['name'],
                            odoo_company=company,
                            score=98.0,
                            match_type='exact',
                            display=f"{r['name']} — {company} (telefon eşleşti)",
                        ))

        # ─── 3. İsim bazlı fuzzy arama ────────────────────────────────────
        # Odoo'dan tüm kişi kontaklarını çek (is_company=False)
        # Büyük sistemlerde sayfalama eklenebilir
        all_contacts = self.models.execute_kw(
            self.db, self.uid, self._api_key,
            'res.partner', 'search_read',
            [[['is_company', '=', False], ['active', '=', True]]],
            {'fields': ['id', 'name', 'parent_id', 'email', 'phone'], 'limit': 2000}
        )

        for contact in all_contacts:
            if contact['id'] in seen_ids:
                continue

            norm_odoo_name = normalize_name(contact['name'])
            name_score = fuzzy_score(norm_name, norm_odoo_name)

            # Şirket adı ek puan katkısı
            company_bonus = 0.0
            odoo_company_raw = contact['parent_id'][1] if contact.get('parent_id') else ''
            if company_name and odoo_company_raw:
                norm_odoo_company = normalize_company(odoo_company_raw)
                company_score = fuzzy_score(norm_company, norm_odoo_company)
                if company_score >= 80:
                    company_bonus = 5.0   # Aynı şirketteyse bonus puan
                elif company_score < 40 and norm_company and norm_odoo_company:
                    company_bonus = -10.0  # Farklı şirketteyse ceza

            total_score = min(100.0, name_score + company_bonus)

            if total_score >= self.MEDIUM_THRESHOLD:
                seen_ids.add(contact['id'])
                if total_score >= self.EXACT_THRESHOLD:
                    match_type = 'exact'
                elif total_score >= self.HIGH_THRESHOLD:
                    match_type = 'high'
                else:
                    match_type = 'medium'

                candidates.append(MatchCandidate(
                    odoo_id=contact['id'],
                    odoo_name=contact['name'],
                    odoo_company=odoo_company_raw,
                    score=round(total_score, 1),
                    match_type=match_type,
                    display=f"{contact['name']} — {odoo_company_raw} (%{total_score:.0f})",
                ))

        # Skora göre sırala
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:10]  # En iyi 10 aday

    def find_company_candidates(self, company_name: str) -> list[MatchCandidate]:
        """Şirket adı için fuzzy arama"""
        norm = normalize_company(company_name)
        companies = self.models.execute_kw(
            self.db, self.uid, self._api_key,
            'res.partner', 'search_read',
            [[['is_company', '=', True], ['active', '=', True]]],
            {'fields': ['id', 'name'], 'limit': 1000}
        )
        candidates = []
        for c in companies:
            score = fuzzy_score(norm, normalize_company(c['name']))
            if score >= self.MEDIUM_THRESHOLD:
                candidates.append(MatchCandidate(
                    odoo_id=c['id'],
                    odoo_name=c['name'],
                    odoo_company='',
                    score=round(score, 1),
                    match_type='exact' if score >= self.EXACT_THRESHOLD else ('high' if score >= self.HIGH_THRESHOLD else 'medium'),
                    display=f"{c['name']} (%{score:.0f})",
                ))
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:5]

    @property
    def _api_key(self):
        # xmlrpc çağrılarında api_key yerine password alanı kullanılır
        # __init__ sırasında kaydedilmesi gerekir — burada placeholder
        return self.__dict__.get('_stored_api_key', '')


def serialize_candidates(candidates: list[MatchCandidate]) -> list[dict]:
    """JSON serileştirme için dict listesi"""
    return [
        {
            'id': c.odoo_id,
            'name': c.odoo_name,
            'company': c.odoo_company,
            'score': c.score,
            'match_type': c.match_type,
            'display': c.display,
        }
        for c in candidates
    ]
