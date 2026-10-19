"""서울 열린데이터광장 원자료 내려받기와 매니페스트 기록.

공식 내려받기 주소에서 원본 파일을 받아 data/raw/에 그대로 저장함. 월별 파일은 갱신될 수 있어
받은 시각·크기·SHA-256을 매니페스트에 남겨 어떤 파일로 분석했는지 추적함. 이미 있는 원본은 덮어쓰지 않음.

인자(여러 개 가능):
    YYMM, YYMMDD: OA-21221(환승 자료) 월별·일별 파일 코드. 예: 2609, 261001
    데이터셋ID:코드[:infSeq]: 다른 데이터셋 첨부파일. infSeq 생략 시 1. 예: OA-15067:58, OA-21231:2:3

입력: 없음(인터넷 연결 필요)
출력: data/raw/<원본 파일명>, data/raw/download_manifest.csv(실패도 한 줄씩 기록)
실행: python scripts/01_download.py 2609 2608 261001 OA-15067:58
"""

import csv
import datetime as dt
import hashlib
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
MANIFEST = RAW / "download_manifest.csv"
PAGE = "https://data.seoul.go.kr/dataList/{}/F/1/datasetView.do"
ENDPOINT = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false"
FIELDS = ["source_id", "seq", "file_name", "official_url", "retrieval_utc", "bytes", "sha256", "status", "notes"]


def download(arg: str) -> dict:
    """파일 하나를 받아 저장하고 매니페스트 한 줄을 돌려줌.

    Args:
        arg: 내려받기 코드. 형식은 모듈 설명 참고.

    Returns:
        매니페스트 열(FIELDS) 이름을 키로 하는 사전. 실패하면 status가 FAIL이고 notes에 오류 내용이 들어감.
    """
    parts = arg.split(":") if ":" in arg else ["OA-21221", arg]
    inf, seq = parts[0], parts[1]
    inf_seq = parts[2] if len(parts) > 2 else "1"  # 데이터셋 페이지 내려받기 양식의 infSeq 값(OA-21231 첨부는 3)
    page = PAGE.format(inf)
    body = urllib.parse.urlencode({"infId": inf, "seqNo": "", "seq": seq, "infSeq": inf_seq}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={"Referer": page, "User-Agent": "Mozilla/5.0"})
    row = {"source_id": inf, "seq": seq, "official_url": page,
           "retrieval_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            cd = r.headers.get("Content-Disposition", "")
            name = cd.split("filename=")[-1].strip('"').encode("latin-1").decode("utf-8") if "filename=" in cd else ""
            if not name.endswith((".zip", ".xlsx", ".csv")):
                raise RuntimeError(f"unexpected response: {cd!r}")
            out = RAW / name
            if out.exists():
                raise RuntimeError(f"{name} already exists; raw files are never overwritten")
            h, n = hashlib.sha256(), 0
            tmp = out.with_suffix(".part")
            with open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
            expected = int(r.headers.get("Content-Length", n))
            if n != expected:
                raise RuntimeError(f"size mismatch {n} != {expected}")
            tmp.rename(out)
        row.update(file_name=name, bytes=n, sha256=h.hexdigest(), status="OK", notes="")
    except Exception as e:  # 실패도 매니페스트에 남겨 누락·재시도 여부를 확인할 수 있게 함
        row.update(file_name="", bytes="", sha256="", status="FAIL", notes=str(e))
    return row


if __name__ == "__main__":
    RAW.mkdir(parents=True, exist_ok=True)
    new = not MANIFEST.exists()
    with open(MANIFEST, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for seq in sys.argv[1:]:
            row = download(seq)
            w.writerow(row)
            f.flush()
            print(row["status"], row["file_name"] or seq, row["bytes"], row["notes"], flush=True)
