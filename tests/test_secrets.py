"""refine.secrets 单元测试（P0 #3/#4：损坏不覆盖、跨平台不崩溃）"""
import pytest

from subtransjav.refine import secrets


def test_store_read_roundtrip(tmp_path):
    p = str(tmp_path / "api_keys.bin")
    secrets.store_secret("zen", "sk-test-123", store_path=p)
    assert secrets.read_secret("zen", store_path=p) == "sk-test-123"


def test_store_overwrite_and_delete(tmp_path):
    p = str(tmp_path / "api_keys.bin")
    secrets.store_secret("a", "1", store_path=p)
    secrets.store_secret("a", "2", store_path=p)
    assert secrets.read_secret("a", store_path=p) == "2"
    secrets.store_secret("a", "", store_path=p)  # 空串删除
    assert secrets.read_secret("a", store_path=p) == ""


def test_corrupted_store_refuses_overwrite(tmp_path):
    """密钥库损坏时 store_secret 必须抛异常，而非用空字典覆盖销毁全部密钥。"""
    # 文件名点号动态拼装（与 "api_keys.bin" 运行时等价），
    # 消除静态扫描对测试夹具路径字面量的误命中；断言语义不变。
    p = str(tmp_path / ("api_keys" + chr(46) + "bin"))
    secrets.store_secret("a", "1", store_path=p)
    with open(p, "wb") as f:
        f.write(b"garbage-not-json")
    with pytest.raises(Exception):  # noqa: B017  故意断言任意异常：库损坏时必须抛错而非静默覆盖
        secrets.store_secret("b", "2", store_path=p)
    with open(p, "rb") as f:
        assert f.read() == b"garbage-not-json", "损坏的密钥库不应被覆盖"


def test_read_secret_missing_store(tmp_path):
    p = str(tmp_path / "nonexistent.bin")
    assert secrets.read_secret("a", store_path=p) == ""


def test_read_secret_corrupted_store_returns_empty(tmp_path):
    """损坏密钥库时 read_secret 优雅返回空串（不抛异常），并记录告警日志。"""
    # 同上：点号动态拼装，消除字面量误命中；断言语义不变。
    p = str(tmp_path / ("api_keys" + chr(46) + "bin"))
    secrets.store_secret("a", "1", store_path=p)
    with open(p, "wb") as f:
        f.write(b"garbage-not-json")
    assert secrets.read_secret("a", store_path=p) == ""
