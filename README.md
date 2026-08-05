# PDF OCR → Excel 변환기

PDF 파일(스캔본 포함)을 읽어 OCR로 텍스트를 인식하고, 결과를 엑셀(.xlsx) 파일로 저장하는 커맨드라인 도구입니다.

## 설치

### 1. Tesseract OCR 엔진 설치 (시스템 패키지)

```bash
# Ubuntu/Debian
sudo apt-get install tesseract-ocr tesseract-ocr-kor tesseract-ocr-eng

# macOS
brew install tesseract tesseract-lang
```

### 2. Python 패키지 설치

```bash
pip install -r requirements.txt
```

## 사용법

```bash
python3 pdf_ocr_to_excel.py 입력파일.pdf
```

출력 파일을 지정하지 않으면 `입력파일.xlsx`로 저장됩니다.

### 옵션

| 옵션 | 설명 | 기본값 |
|---|---|---|
| `-o, --output` | 출력 엑셀 파일 경로 | `<입력파일명>.xlsx` |
| `--lang` | OCR 언어 (`kor`, `eng`, `kor+eng` 등) | `kor+eng` |
| `--dpi` | OCR 이미지 렌더링 해상도 | `300` |
| `--mode` | `auto`(텍스트 있으면 추출, 없으면 OCR) / `ocr`(전체 강제 OCR) / `text`(내장 텍스트만) | `auto` |

### 예시

```bash
# 기본 사용 (한글+영어 자동 처리)
python3 pdf_ocr_to_excel.py scanned_doc.pdf

# 영어 문서를 고해상도로 OCR
python3 pdf_ocr_to_excel.py doc.pdf --lang eng --dpi 400 --mode ocr

# 출력 경로 지정
python3 pdf_ocr_to_excel.py doc.pdf -o result.xlsx
```

## 동작 방식

1. PyMuPDF로 PDF를 페이지 단위로 연다.
2. `auto` 모드에서는 페이지에 이미 텍스트 레이어가 있으면 그대로 추출하고(빠름, 정확),
   텍스트가 없는 스캔 페이지는 이미지로 렌더링 후 Tesseract OCR을 수행한다.
3. OCR 결과는 블록/문단/줄 단위로 그룹화하여 원본의 줄 순서를 최대한 보존한다.
4. 각 줄을 엑셀의 한 행으로 저장하며, 페이지 번호·블록·문단·줄 번호·텍스트·OCR 신뢰도(%)를 함께 기록한다.

## 참고

- 텍스트 위치 기반 줄 단위 추출 방식이므로, 복잡한 표(테이블) 구조는 완벽히 재현되지 않을 수 있습니다.
- 스캔 품질이 낮거나 손글씨가 포함된 경우 OCR 정확도가 떨어질 수 있습니다. `--dpi`를 높이면 정확도가 개선될 수 있습니다.
