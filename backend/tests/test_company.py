import pytest

from app.company import identify, normalize_name


@pytest.mark.parametrize(
    "name, key",
    [
        ("Acme", "acme"),
        ("ACME, Inc.", "acme"),
        ("Acme S.A.", "acme"),
        ("Société Générale SA", "societe generale"),
        ("OCP Group", "ocp"),
        ("Deloitte Conseil SARL", "deloitte conseil"),
        ("Group", "group"),  # a suffix alone is kept
    ],
)
def test_normalize_name(name, key):
    assert normalize_name(name) == key


@pytest.mark.parametrize(
    "headline, company",
    [
        ("Backend engineer at Acme | Go, Kafka", "Acme"),
        ("Ingénieure logiciel chez Société Générale", "Société Générale"),
        ("SWE @ Stripe", "Stripe"),
        ("Développeur à OCP, Casablanca", "OCP"),
        ("مهندس برمجيات لدى إنوي", "إنوي"),
    ],
)
def test_company_from_headline(headline, company):
    ref = identify(headline, "")
    assert ref.name == company and ref.source == "headline"


def test_order_override_then_page_then_headline_then_experience():
    raw = "Jane\nAbout\nHello\nExperience\nBackend intern\nInwi · Internship\nJan 2024"
    hint = {"name": "Globex", "linkedin_slug": "globex-corp"}
    assert identify("Engineer at Acme", raw, hint, override="Initech").name == "Initech"
    page = identify("Engineer at Acme", raw, hint)
    assert (page.name, page.linkedin_slug, page.source) == ("Globex", "globex-corp", "page")
    assert identify("Engineer at Acme", raw, {"name": ""}).source == "headline"
    exp = identify("Backend engineer", raw)
    assert (exp.name, exp.source) == ("Inwi", "experience")


def test_no_company():
    assert identify("Student", "Jane\nStudent") is None
    assert identify("Freelance", "", {"name": "Self-employed"}) is None
    assert identify("Developer at Freelance", "") is None
    # A bad slug from the page is ignored, not trusted.
    assert identify("Engineer at Acme", "", {"name": "Acme", "linkedin_slug": "../../x"}).linkedin_slug is None
