import pytest
from app import create_app
from app.models import db, Event, Church, Participant, Draw, Assignment, RetrievalCredential
from app.services.bunotan import generate_assignments, validate_assignments, DrawError
from app.services.draw_service import cancel_draw, run_draw
from app.services.pins import new_pin, new_reference, hash_pin, verify_pin


@pytest.mark.parametrize("n", [2, 3, 10, 100, 500])
def test_draw_valid(n):
    ids = list(range(n))
    assert validate_assignments(ids, generate_assignments(ids)) == []


@pytest.mark.parametrize("n", [0, 1])
def test_too_few(n):
    with pytest.raises(DrawError):
        generate_assignments(list(range(n)))


def test_duplicates_rejected():
    with pytest.raises(DrawError):
        generate_assignments([1, 1, 2])


def test_validator_catches_problems():
    assert validate_assignments([1, 2], {1: 1, 2: 2})
    assert validate_assignments([1, 2, 3], {1: 2, 2: 2, 3: 1})


def test_pin_hash():
    pin = new_pin()
    h = hash_pin(pin)
    assert h != pin and verify_pin(h, pin) and not verify_pin(h, "000000")


@pytest.fixture
def ctx():
    app = create_app(
        {
            "SECRET_KEY": "test-secret",
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
        }
    )
    with app.app_context():
        db.create_all()
        yield


def _seed(n, status="ready_for_drawing"):
    ev = Event(name="Demo", status=status)
    ch = Church(name="Demo Church")
    db.session.add_all([ev, ch]); db.session.flush()
    for i in range(n):
        participant = Participant(
            event_id=ev.id,
            church_id=ch.id,
            full_name=f"P{i}",
            name_key=f"p{i}",
            status="approved",
            participation_confirmed=True,
            privacy_consent=True,
        )
        db.session.add(participant)
        db.session.flush()
        db.session.add(
            RetrievalCredential(
                participant_id=participant.id,
                reference=new_reference(),
                pin_hash=hash_pin(new_pin()),
            )
        )
    db.session.commit()
    return ev


def test_run_draw_persists_and_supersedes(ctx):
    ev = _seed(10)
    d1 = run_draw(ev)
    assert Assignment.query.filter_by(draw_id=d1.id).count() == 10
    with pytest.raises(DrawError, match="Supersede"):
        run_draw(ev)
    cancel_draw(ev, "Administrator approved a replacement draw")
    assert db.session.get(Draw, d1.id).status == "superseded"
    ev.status = "ready_for_drawing"
    d2 = run_draw(ev)
    assert d2.version == 2 and db.session.get(Draw, d1.id).status == "superseded"


def test_run_draw_wrong_status(ctx):
    with pytest.raises(DrawError):
        run_draw(_seed(5, status="draft"))


def test_run_draw_needs_two(ctx):
    with pytest.raises(DrawError):
        run_draw(_seed(1))
