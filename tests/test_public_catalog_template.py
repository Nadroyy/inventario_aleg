from pathlib import Path


def test_public_catalog_consumes_token_only_once():
    template = Path(__file__).resolve().parents[1] / 'proyecto_web' / 'templates' / 'catalogo_cliente.html'
    html = template.read_text(encoding='utf-8')

    consume_calls = html.count("fetch(`/consume-token/${t}`") + html.count("fetch(`/consume-token/${token}`")

    assert consume_calls == 1
