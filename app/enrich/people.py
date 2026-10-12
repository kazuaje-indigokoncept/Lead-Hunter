"""Candidatos profesionales publicados y vínculos explícitos con organizaciones."""
from html.parser import HTMLParser
from urllib.parse import urljoin
from .. import companies, intelligence


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items, self.anchor, self.parts = [], None, []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.anchor, self.parts = dict(attrs).get("href"), []
    def handle_data(self, data):
        if self.anchor:
            self.parts.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            self.items.append((self.anchor, " ".join(self.parts).strip()))
            self.anchor = None


def extract(html, url, source="website"):
    from . import website
    parser = website.PageParser()
    parser.feed(html)
    candidates = []
    nodes = {}
    def index(n):
        if isinstance(n, list):
            for item in n:
                index(item)
        elif isinstance(n, dict):
            if isinstance(n.get("@id"), str) and len(n) > len(nodes.get(n["@id"], {})):
                nodes[n["@id"]] = n
            for value in n.values():
                if isinstance(value, (dict, list)):
                    index(value)
    index(parser.structured)
    def resolve(org):
        if isinstance(org, list):
            return [resolve(o) for o in org]
        if isinstance(org, dict) and "@id" in org:
            return nodes.get(org["@id"], org)
        return org
    def org_label(org):
        if isinstance(org, list):
            return "; ".join(org_label(o) for o in org)
        if isinstance(org, dict):
            return " · ".join(str(org.get(k, "")) for k in ("name", "url", "@id") if isinstance(org.get(k), str))[:500]
        return str(org or "Sin organización publicada")[:500]
    def walk(n, parent_org=None):
        if isinstance(n, list):
            for item in n:
                walk(item, parent_org)
        elif isinstance(n, dict):
            types = n.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if "Person" in types and isinstance(n.get("name"), str):
                links = n.get("sameAs", [])
                links = list(links) if isinstance(links, list) else [links]
                links += [n.get("url", "")]
                linkedin = next((companies.linkedin_url(u, "in") for u in links if isinstance(u, str) and companies.linkedin_url(u, "in")), "")
                emails = n.get("email", [])
                emails = emails if isinstance(emails, list) else [emails]
                emails = [v.removeprefix("mailto:") for v in emails if isinstance(v, str)]
                phones = n.get("telephone", [])
                phones = phones if isinstance(phones, list) else [phones]
                phones = [v for v in phones if isinstance(v, str)]
                role = n.get("jobTitle", "")
                role = role if isinstance(role, str) else ""
                org = resolve(n.get("worksFor", parent_org))
                candidates.append({"name": n["name"], "role": role, "linkedin": linkedin,
                    "emails": website._clean_emails(emails, "")[:5], "phones": [p for p in (website.clean_phone(v) for v in phones) if p][:5],
                    "organization": org, "evidence_records": [intelligence.evidence(
                        f"{n['name']} · {role} · organización publicada: {org_label(org)}", source, url)]})
            for key, value in n.items():
                if isinstance(value, (dict, list)):
                    organization = n if key in ("employee", "founder") else None
                    walk(value, organization)
    walk(parser.structured)
    links = Links()
    links.feed(html)
    seen = {c["linkedin"] for c in candidates if c["linkedin"]}
    for href, text in links.items:
        profile = companies.linkedin_url(urljoin(url, href), "in")
        if profile and profile not in seen:
            name = text if text and text.lower() not in ("linkedin", "perfil", "ver perfil", "profile") and len(text) < 100 else ""
            candidates.append({"name": name, "role": "", "linkedin": profile, "organization": None,
                "evidence_records": [intelligence.evidence("Perfil enlazado; empleo y cargo pendientes de confirmar", source, url)]})
            seen.add(profile)
    return candidates[:30]


def store(cid, candidates):
    company = companies.context(cid)
    saved = []
    for candidate in candidates:
        org = candidate.get("organization")
        if org and not companies.relation_matches(org, company):
            continue
        data = {**candidate, "relationship": "published" if org else "pending"}
        if data.get("name") or data.get("linkedin"):
            saved.append(companies.save_contact(cid, data))
    return saved
