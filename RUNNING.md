# bgpy_pkg 실행 가이드

BGPy 시뮬레이터와 이 저장소의 path-filter 빌드 스크립트를 실행하는 방법입니다.

## 1. 환경 준비

이 저장소는 이미 `../bgpy_venv` (PyPy 3.10) 가상환경에 editable 모드로 설치되어 있습니다.

```bash
# 저장소 루트에서
cd ~/bgpy_pkg
source ../bgpy_venv/bin/activate
```

처음부터 새로 설치하는 경우:

```bash
python -m venv ../bgpy_venv
source ../bgpy_venv/bin/activate
pip install -e .          # pyproject.toml 기반 editable 설치
pip install pybloomfiltermmap3 numpy pandas
# 또는: pip install -r requirements.txt
```

요구사항: Python >= 3.10 (CPython 또는 PyPy).

설치 확인:

```bash
python -c "import bgpy; print('bgpy OK', bgpy.__file__)"
```

## 2. UpPathDB & UpPathFilter 제작

Valid upward path segment를 SQLite DB로 생성하고, 그 DB를 바탕으로 bloom filter를 제작하는 과정입니다.
Max_path_len = 11로 설정해서 돌릴 경우 대략 40 시간 정도가 소모되고, 해당 작업은 시간에 따라 달라지는 값이기에 발표때 사용한 DB와는 다른 DB가 만들어집니다. 

**따라서 여기서는 방법만 설명하고, 이후 단계에서 사용할 UpPathFilter 파일들을 따로 올리겠습니다.**

```bash
# UpPathDB 제작 (-m 11 기준 40시간 소요)
python3 bgpy/build_path_filter.py 1 -m 8
python3 bgpy/build_path_filter.py 1 -m 9
python3 bgpy/build_path_filter.py 1 -m 10
python3 bgpy/build_path_filter.py 1 -m 11
```

```bash
# UpPathFilter 제작 (-m 11 기준 3.28시간 소요)
python3 bgpy/build_pybloom_filter.py build -m 8
python3 bgpy/build_pybloom_filter.py build -m 9
python3 bgpy/build_pybloom_filter.py build -m 10
python3 bgpy/build_pybloom_filter.py build -m 11
```

| 파일 | 설명 |
|------|------|
| `bgpy/bgpy_path_segments.sqlite3` | 길이 ≥2 의 upward path 세그먼트 |
| `bgpy/bgpy_path_filter.bloom` | 경로 멤버십 Bloom filter |
| `bgpy/bgpy_path_filter*.meta.json` | Bloom filter 메타데이터 |

> 첫 실행 시 CAIDA AS-graph 를 내려받아 캐시합니다. 이후 실행은 캐시를 재사용합니다.

<!-- ## 3. Path Filter 빌드 (이 저장소 커스텀 작업)

CAIDA AS 토폴로지에서 upward path 세그먼트를 뽑아 SQLite + Bloom filter 로 만드는 파이프라인입니다.

```bash
# (1) upward path 세그먼트를 SQLite DB로 생성
#     -> bgpy/bgpy_path_segments.sqlite3
python bgpy/build_path_filter.py

# (2) 세그먼트 DB로부터 Bloom filter 생성
#     -> bgpy/bgpy_path_filter*.bloom (+ .meta.json)
python bgpy/build_pybloom_filter.py
```

생성되는 산출물:

| 파일 | 설명 |
|------|------|
| `bgpy/bgpy_path_segments.sqlite3` | 길이 ≥2 의 upward path 세그먼트 |
| `bgpy/bgpy_path_filter.bloom` | 경로 멤버십 Bloom filter |
| `bgpy/bgpy_path_filter*.meta.json` | Bloom filter 메타데이터 |

> 첫 실행 시 CAIDA AS-graph 를 내려받아 캐시합니다. 이후 실행은 캐시를 재사용합니다. -->

## 3. 분석 / 플롯 스크립트

`scripts/` 에 측정·플롯 스크립트가 있습니다.

```bash
python scripts/measure_filter_coverage_multi_mp.py   # 필터 커버리지 측정
# scripts/filter_coveragte_path.csv 파일 생성됨
python scripts/measure_aspawn_lookups.py             # ASPAwN filter 시간 측정
# scripts/aspawn_lookups.csv 파일 생성
python scripts/plot_filter_coverage.py               # 결과 그래프 생성 (scripts/plots/)
```

## 참고

- 원본 BGPy 문서/튜토리얼: [README.md](README.md) 및 [BGPy Wiki](https://github.com/jfuruness/bgpy/wiki/Tutorial)
- 대용량 산출물(`.sqlite3`, `.bloom`)은 GitHub 100MB 제한에 걸릴 수 있으니 Git LFS 또는 외부 스토리지 사용을 권장합니다.
