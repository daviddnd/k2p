"""The licensed-pack consumption seam in the form registry (ADR-0012).

``load_form_registry`` is the one place a form's structure is read (ADR-0006's
"one seam" rule), so it is the one place a licensed ``form_mapping`` pack can
come to matter. This suite pins what the seam does without deciding the open
question it is built for:

- a pack payload builds exactly the registry the image holds — the three
  documents per form (manifest, mapping, geometry) round-trip, so a pack
  carrying the image's own content produces the same extraction behaviour;
- a pack's entry wins when a caller supplies one, including when its mapping
  differs — that is what "preferred over the in-image registry" means;
- a form the pack does not carry, and no payload at all, fall back to the
  image — the path every caller takes today;
- a malformed pack entry is a hard error, like a malformed registry file.

**No caller passes a payload yet**, and this suite does not change that: the
consumers are synchronous and the pack cache is a database read, and whether
the base image may hold mapping intelligence at all is the founder's open
decision (ADR-0012 says "not present in the base image"; ADR-0006/0010 ship
``backend/forms/`` in it). The seam exists so that decision can be taken
without rewriting this loader — see the module docstring.
"""

import json
from pathlib import Path

import pytest

from app.adapters.forms.registry import (
    FormRegistry,
    FormRegistryError,
    forms_root,
    image_registry,
    load_form_registry,
    registry_from_pack,
)
from app.domain.prior_filing import FormCode

FORMS = [FormCode.NAR1, FormCode.ND2A]


def form_entry(form: FormCode) -> dict[str, object]:
    """One form's three registry documents, as a pack would carry them."""
    directory = forms_root() / form.value
    return {
        "manifest": json.loads((directory / "manifest.json").read_text(encoding="utf-8")),
        "mapping": json.loads((directory / "mapping.json").read_text(encoding="utf-8")),
        "geometry": json.loads((directory / "geometry.json").read_text(encoding="utf-8")),
    }


def pack_payload(**overrides: dict[str, object]) -> dict[str, object]:
    forms: dict[str, object] = {form.value: form_entry(form) for form in FORMS}
    forms.update(overrides)
    return {"forms": forms}


def test_a_pack_carrying_the_registry_reproduces_it() -> None:
    """The seam's round trip: image content in, the same registry out."""
    form = FormCode.NAR1
    packed = registry_from_pack(form, pack_payload())

    assert packed is not None
    image = image_registry(form)
    assert packed == image
    assert packed.template == image.template
    assert packed.template_sha256 == image.template_sha256
    assert (Path(forms_root() / form.value / packed.template)).is_file()


@pytest.mark.parametrize("form", FORMS)
def test_every_pinned_form_round_trips(form: FormCode) -> None:
    packed = registry_from_pack(form, pack_payload())

    assert packed is not None
    assert packed.items == image_registry(form).items
    assert set(packed.geometry) == set(image_registry(form).geometry)


def test_a_pack_entry_is_preferred_over_the_image() -> None:
    """A cached pack wins for the forms it carries — that is the seam's point."""
    payload = pack_payload(
        nar1={
            **form_entry(FormCode.NAR1),
            "mapping": {
                "items": [
                    {
                        "item": "1",
                        "type": "text",
                        "target": "company.name_en + company.name_zh",
                        "pdf_fields": ["fill_1_P"],
                    }
                ]
            },
            "geometry": {
                "fields": [
                    {"name": "fill_1_P", "page": 1, "rect": [1, 2, 3, 4], "type": "text"}
                ]
            },
        }
    )

    chosen = load_form_registry(FormCode.NAR1, pack_payload=payload)

    assert chosen.items[0].pdf_fields == ("fill_1_P",)
    assert chosen.geometry["fill_1_P"].rect == (1.0, 2.0, 3.0, 4.0)
    # The image is still there and still what an unqualified call reads.
    assert load_form_registry(FormCode.NAR1) == image_registry(FormCode.NAR1)


def test_a_form_the_pack_does_not_carry_falls_back_to_the_image() -> None:
    payload = {"forms": {"nar1": form_entry(FormCode.NAR1)}}

    assert load_form_registry(FormCode.ND2A, pack_payload=payload) == image_registry(FormCode.ND2A)
    assert registry_from_pack(FormCode.ND2A, payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"nope": {}}, id="no-forms-object"),
        pytest.param({"forms": {"nar1": "not an object"}}, id="entry-not-an-object"),
        pytest.param(
            {"forms": {"nar1": {"manifest": {}, "mapping": {"items": []}}}},
            id="missing-section",
        ),
        pytest.param(
            {
                "forms": {
                    "nar1": {
                        "manifest": form_entry(FormCode.NAR1)["manifest"],
                        "mapping": {
                            "items": [
                                {
                                    "item": "1",
                                    "type": "text",
                                    "target": "company.name_en",
                                    "pdf_fields": ["fill_1_P"],
                                }
                            ]
                        },
                        "geometry": {"fields": []},
                    }
                }
            },
            id="geometry-does-not-match-the-mapping",
        ),
    ],
)
def test_a_malformed_pack_entry_is_a_hard_error(payload: dict[str, object]) -> None:
    """Never half-read: a pack that cannot be trusted is refused like a bad file."""
    with pytest.raises(FormRegistryError):
        registry_from_pack(FormCode.NAR1, payload)


def test_the_image_path_is_untouched_by_the_seam() -> None:
    """The default call — the one every caller makes — reads the image."""
    cached = load_form_registry(FormCode.NAR1)

    assert cached == image_registry(FormCode.NAR1)
    # And the cache still returns one shared object, not a fresh parse.
    assert load_form_registry(FormCode.NAR1) is cached


def test_the_registry_type_is_unchanged_by_the_seam() -> None:
    """Pack or image, a consumer gets exactly the same value object."""
    assert isinstance(load_form_registry(FormCode.NAR1), FormRegistry)
    assert load_form_registry(FormCode.NAR1).form is FormCode.NAR1
