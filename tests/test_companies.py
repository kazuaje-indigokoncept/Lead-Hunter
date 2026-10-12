import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app import db, companies, jobs, intelligence
from app.enrich import people, website, linkedin
import test_maps_api

PROFILE = "https://www.linkedin.com/in/ana/"


def person_html(org="Acme", role="Directora de marketing", relation="worksFor"):
    node = {"@type": "Person", "name": "Ana Ruiz", "jobTitle": role, "sameAs": PROFILE,
            "email": "ana@acme.test", relation: {"@type": "Organization", "name": org}}
    return '<title>Ana Ruiz</title><script type="application/ld+json">'+json.dumps(node)+'</script>'


class CompanyApiTests(unittest.TestCase):
    setUp = test_maps_api.CampaignApiTests.setUp
    tearDown = test_maps_api.CampaignApiTests.tearDown

    def campaign(self, name="Prueba"):
        self.body["name"] = name
        return self.client.post("/api/campaigns", json=self.body).json()["id"]

    def lead(self, cid, key, **values):
        lid = db.insert("leads", {"campaign_id": cid, "dedupe_key": key, "source": "google_maps", "name": "Acme",
                                  "website": "https://acme.test", **values})
        company = companies.attach(db.one("SELECT * FROM leads WHERE id=?", (lid,)))
        return lid, company

    def test_cross_campaign_exact_identity_groups_without_losing_records(self):
        lid, cid = self.lead(self.campaign(), "first", city="Bogotá")
        other_lid, other_cid = self.lead(self.campaign("Segunda"), "second", city="Medellín", website="https://www.acme.test/contacto")
        self.assertEqual(cid, other_cid)
        listing = self.client.get("/api/companies?sales_profile=web").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["record_count"], 2)
        self.assertEqual(listing["items"][0]["campaign_count"], 2)
        info = self.client.get(f"/api/companies/{cid}").json()
        self.assertEqual({r["id"] for r in info["records"]}, {lid, other_lid})
        self.assertIn("Empresa agrupada", self.client.get("/api/export?fmt=csv").text)

    def test_similar_names_shared_domain_and_conflicting_linkedin_do_not_merge(self):
        campaign = self.campaign()
        _, one = self.lead(campaign, "a", linkedin="https://linkedin.com/company/acme")
        _, two = self.lead(campaign, "b", website="https://other.test")
        _, three = self.lead(campaign, "c", name="Otro negocio")
        _, four = self.lead(campaign, "d", linkedin="https://linkedin.com/company/different")
        self.assertEqual(len({one,two,three,four}), 4)

    def test_manual_assignment_and_split_are_persistent(self):
        campaign = self.campaign()
        _, company = self.lead(campaign, "a")
        lid, other = self.lead(campaign, "b", website="https://different.test")
        response = self.client.post(f"/api/leads/{lid}/company", json={"company_id": company})
        self.assertEqual(response.json()["company_id"], company)
        row = db.one("SELECT * FROM leads WHERE id=?", (lid,))
        self.assertEqual(companies.attach(row), company)
        split = self.client.post(f"/api/leads/{lid}/company", json={}).json()
        self.assertNotEqual(split["company_id"], company)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM leads")["n"], 2)

    def test_structured_people_only_match_their_company_and_keep_person_contacts_separate(self):
        lid, cid = self.lead(self.campaign(), "a")
        html = person_html()+person_html("Otra empresa", "CEO")
        extracted = people.extract(html, "https://acme.test/team")
        people.store(cid, extracted)
        contacts = db.query("SELECT * FROM company_contacts WHERE company_id=?", (cid,))
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["emails"], ["ana@acme.test"])
        self.assertEqual(contacts[0]["relationship"], "published")
        self.assertNotIn("ana@acme.test", website.extract(html, "https://acme.test/team")["emails"])
        self.assertEqual(contacts[0]["evidence_records"][0]["url"], "https://acme.test/team")

    def test_plain_link_is_candidate_and_former_job_not_current_relation(self):
        _, cid = self.lead(self.campaign(), "a")
        html = '<a href="'+PROFILE+'">Ana Ruiz</a>'
        people.store(cid, people.extract(html, "https://acme.test/team"))
        contact = companies.detail(cid, "social")["contacts"][0]
        self.assertEqual(contact["relationship"], "pending")
        self.assertEqual(contact["decision_priority"], 0)
        company = companies.context(cid)
        response = httpx.Response(200, text=person_html(relation="alumniOf"), headers={"content-type":"text/html"})
        with patch.object(website, "fetch_page", AsyncMock(return_value=(response, PROFILE, None))):
            status = asyncio.run(linkedin.review_person(None, contact, company))
        self.assertEqual(status, "relationship_unconfirmed")
        self.assertEqual(db.one("SELECT * FROM company_contacts WHERE id=?", (contact["id"],))["relationship"], "pending")

    def test_public_profile_requires_matching_employer_and_preserves_on_block(self):
        _, cid = self.lead(self.campaign(), "a")
        contact_id = companies.save_contact(cid, {"name":"Ana", "linkedin":PROFILE})
        company = companies.context(cid)
        contact = db.one("SELECT * FROM company_contacts WHERE id=?", (contact_id,))
        response = httpx.Response(200, text=person_html(), headers={"content-type":"text/html"})
        with patch.object(website,"fetch_page",AsyncMock(return_value=(response,PROFILE,None))):
            self.assertEqual(asyncio.run(linkedin.review_person(None,contact,company)),"public_page")
        saved = db.one("SELECT * FROM company_contacts WHERE id=?", (contact_id,))
        self.assertEqual(saved["role"], "Directora de marketing")
        self.assertEqual(saved["relationship"], "published")
        blocked = httpx.Response(403)
        with patch.object(website,"fetch_page",AsyncMock(return_value=(blocked,PROFILE,None))):
            self.assertEqual(asyncio.run(linkedin.review_person(None,saved,company)),"access_restricted")
        saved = db.one("SELECT * FROM company_contacts WHERE id=?", (contact_id,))
        self.assertEqual(saved["emails"], ["ana@acme.test"])
        self.assertEqual(saved["review_data"]["status"], "access_restricted")

    def test_manual_decision_precedes_new_extraction_and_ranking_depends_on_service(self):
        _, cid = self.lead(self.campaign(), "a")
        contact = self.client.post(f"/api/companies/{cid}/contacts",json={"name":"Ana", "role":"Directora de marketing", "linkedin":PROFILE, "relationship":"confirmed"}).json()
        self.assertIn("id", contact)
        companies.save_contact(cid, {"name":"Ana", "role":"Directora de marketing", "linkedin":PROFILE, "relationship":"pending"})
        web=companies.detail(cid,"web")["contacts"][0]
        social=companies.detail(cid,"social")["contacts"][0]
        self.assertEqual(social["relationship"],"confirmed")
        self.assertGreater(social["decision_priority"],web["decision_priority"])
        response=self.client.patch(f"/api/company-contacts/{contact['id']}",json={"relationship":"rejected"})
        self.assertEqual(response.status_code,200)
        companies.save_contact(cid, {"linkedin":PROFILE, "relationship":"published"})
        self.assertEqual(companies.detail(cid)["contacts"][0]["decision_priority"],0)

    def test_graph_reference_and_employer_domain_conflict(self):
        _, cid = self.lead(self.campaign(), "a")
        graph={"@graph":[{"@id":"org", "@type":"Organization", "name":"Acme", "url":"https://acme.test"},
            {"@type":"Person", "name":"Ana", "jobTitle":"CEO", "worksFor":{"@id":"org"}, "sameAs":PROFILE}]}
        html='<script type="application/ld+json">'+json.dumps(graph)+'</script>'
        people.store(cid,people.extract(html,"https://acme.test/team"))
        self.assertEqual(companies.detail(cid)["contacts"][0]["relationship"],"published")
        self.assertFalse(companies.relation_matches({"name":"Acme","url":"https://unrelated.test"},companies.context(cid)))

    def test_missing_contacts_invalid_links_demo_and_limits(self):
        _, cid=self.lead(self.campaign(),"a")
        self.assertEqual(self.client.post(f"/api/companies/{cid}/contacts",json={}).status_code,400)
        self.assertEqual(self.client.post(f"/api/companies/{cid}/contacts",json={"linkedin":"https://linkedin.com.evil.test/in/ana"}).status_code,422)
        self.assertEqual(self.client.get("/api/companies?sales_profile=bad").status_code,422)
        self.assertEqual(self.client.post(f"/api/companies/{cid}/review-decision-makers?sales_profile=bad").status_code,422)
        response=self.client.post(f"/api/companies/{cid}/review-decision-makers?sales_profile=social")
        self.assertEqual(response.status_code,200)
        contact=response.json()["company"]["contacts"][0]
        self.assertEqual(contact["evidence_records"][0]["verification"],"demo")

    def test_campaign_associates_discovered_linkedin_people_to_maps_company(self):
        campaign=self.campaign()
        lid,cid=self.lead(campaign,"a")
        db.set_setting("demo_mode","0")
        data={"web_audit":{"status":"ok"},"linkedin":"https://www.linkedin.com/company/acme/",
            "people_candidates":people.extract(person_html(),"https://acme.test/team")}
        with patch.object(website,"enrich",AsyncMock(return_value=data)):
            asyncio.run(jobs.enrich_lead(None,db.one("SELECT * FROM leads WHERE id=?",(lid,))))
        self.assertEqual(db.one("SELECT * FROM leads WHERE id=?",(lid,))["company_id"],cid)
        self.assertEqual(companies.detail(cid)["contacts"][0]["name"],"Ana Ruiz")

    def test_company_page_discovers_then_checks_person_with_matching_employer(self):
        _,cid=self.lead(self.campaign(),"a",linkedin="https://www.linkedin.com/company/acme/")
        visited=[]
        async def fetch(client,url):
            visited.append(url)
            html='<a href="'+PROFILE+'">Ana Ruiz</a>' if '/company/' in url else person_html()
            return httpx.Response(200,text=html,headers={"content-type":"text/html"}),url,None
        with patch.object(website,"fetch_page",fetch),patch.object(asyncio,"sleep",AsyncMock()):
            result=asyncio.run(linkedin.review_company_people(None,cid))
        self.assertEqual(result["reviewed"],1)
        self.assertEqual(visited,["https://www.linkedin.com/company/acme/",PROFILE])
        contact=companies.detail(cid,"social")["contacts"][0]
        self.assertEqual(contact["relationship"],"published")
        self.assertEqual(contact["decision_priority"],95)
        self.assertEqual(contact["evidence_records"][-1]["url"],PROFILE)

    def test_http_429_stops_remaining_profiles_and_keeps_people(self):
        _,cid=self.lead(self.campaign(),"a")
        companies.save_contact(cid,{"name":"Ana","linkedin":PROFILE})
        companies.save_contact(cid,{"name":"Luis","linkedin":"https://www.linkedin.com/in/luis/"})
        response=httpx.Response(429)
        with patch.object(website,"fetch_page",AsyncMock(return_value=(response,PROFILE,None))) as fetch:
            result=asyncio.run(linkedin.review_company_people(None,cid))
        self.assertEqual(result["status"],"access_restricted")
        self.assertEqual(result["reviewed"],1)
        fetch.assert_awaited_once()
        self.assertEqual(len(companies.detail(cid)["contacts"]),2)


class DecisionMakerRankTests(unittest.TestCase):
    def test_owner_digital_marketing_and_pending(self):
        contacts=[{"role":role,"relationship":"published"} for role in ("Dueño", "CTO", "Directora de marketing", "Administrativo")]
        web=[companies.prioritize(c,"web")["decision_priority"] for c in contacts]
        social=[companies.prioritize(c,"social")["decision_priority"] for c in contacts]
        self.assertGreater(web[1],web[2])
        self.assertGreater(social[2],social[1])
        self.assertGreater(web[0],web[3])
        self.assertEqual(companies.prioritize({"role":"CEO","relationship":"pending"},"web")["decision_priority"],0)
