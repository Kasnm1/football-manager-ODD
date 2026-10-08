[English](GETTING_STARTED.md) · [简体中文](GETTING_STARTED.zh-CN.md) · [한국어](GETTING_STARTED.ko-KR.md)

# 개발 버전 실행하기

[← 프로젝트 홈](../../README.ko-KR.md) · [기여 안내](../../CONTRIBUTING.md)

## 환경 준비

Windows와 Python 3.10 이상을 사용하세요. Python 3.10에는 `tomli`도 필요합니다. 네이티브 코어에는 Rust MSVC 도구 체인, Visual C++ Build Tools와 Windows SDK가 필요합니다.

저장소를 복제하거나 다운로드하고 저장소 루트에서 터미널을 여세요. 개발에는 독립된 Python 환경을 권장합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Python 3.10을 사용하는 경우:

```powershell
python -m pip install tomli
```

## 네이티브 코어 빌드 및 로컬 서비스 시작

```powershell
python scripts\build_rust_native.py
python scripts\build_cpp_hook_core.py
python fm_odds_web.py --port 7857 --no-browser --keep-alive
```

브라우저에서 **http://127.0.0.1:7857**을 여세요. 개발 서비스는 `web/`의 HTML, CSS, JavaScript와 리소스를 직접 읽습니다.

Football Manager를 실행해 세이브를 불러온 뒤 FMODD에서 연결하세요. 사용 가능한 기능은 정확한 게임 빌드와 배포 플랫폼에 따라 달라집니다. 실제 게임 데이터를 변경하는 기능을 처음 사용하기 전에 세이브를 백업해 주세요.

## 수정 시작하기

[기여자 규칙](../../AGENTS.md), [문서 안내](../README.md)와 관련 아키텍처 또는 기능 색인 항목을 먼저 읽으세요. 수정 범위를 좁게 유지하고 해당 변경을 검증할 수 있는 검사를 실행하세요.

프론트엔드 파일을 수정한 뒤에는 브라우저를 새로고침하세요. 서버 코드가 변경되면 로컬 서비스를 재시작하되, 직접 시작한 서비스 인스턴스만 종료하세요. 개인 세이브, 계정 데이터, 자격 증명과 로컬 생성 파일은 커밋에 포함하지 않도록 관리하세요.

상세 개발 및 문제 해결 안내는 [개발 설명서(중국어)](../development/DEVELOPMENT.md)를 참고하세요.
