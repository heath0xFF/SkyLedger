from pathlib import Path


ASSETS = Path(__file__).parents[1] / "skyledger" / "static" / "assets"


def test_database_backed_tables_do_not_render_with_inner_html():
    for name in ("history.js", "aircraft.js"):
        source = (ASSETS / name).read_text()
        assert ".innerHTML" not in source
        assert ".textContent" in source


def test_dynamic_map_tiles_are_created_as_dom_elements():
    source = (ASSETS / "dashboard.js").read_text()
    assert "mapTiles.innerHTML" not in source
    assert 'document.createElement("img")' in source
    assert "mapTiles.replaceChildren" in source
