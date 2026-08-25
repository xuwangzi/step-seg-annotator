from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from stepseg.topology import _build_body


def test_box_face_index_and_candidates() -> None:
    body = _build_body(BRepPrimAPI_MakeBox(10, 10, 10).Shape(), 1)
    seed = body.face_ids[0]
    assert len(body.face_ids) == 6
    assert body.candidate("seed", seed) == {seed}
    assert body.candidate("same_surface", seed) == {seed}
    assert body.candidate("connected", seed) == set(body.face_ids)
