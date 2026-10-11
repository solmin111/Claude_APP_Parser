# Claude Desktop 아티팩트 파서

Windows 디스크 이미지(E01, RAW)에서 Claude Desktop의 사용자 행위와 대화를 복원한다.
이미지는 마운트하지 않고 읽기 전용으로만 연다. BitLocker로 잠긴 볼륨은 키를 받아 풀어서
읽고, 이미지 없이 지금 돌아가는 Windows PC를 바로 분석하는 라이브 모드도 있다.

설치 형태는 세 가지가 있다.

```
%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\   (MSIX)
%APPDATA%\Claude\                                                        (일반 설치)
%LOCALAPPDATA%\Claude\ + %LOCALAPPDATA%\Claude-Data\                     (데이터 분리형)
```

읽는 저장소는 IndexedDB(대화 본문), HTTP 캐시, Local/Session Storage, 설정 파일,
Electron 앱 로그, 프리패치, 그리고 `$UsnJrnl:$J`다.

## 설치

Python은 3.10에서 3.12까지만 된다. `pytsk3`와 `libewf-python`의 Windows 휠이 이 범위에만
올라와 있어서다.

```
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

| 패키지 | 역할 | 없으면 |
| --- | --- | --- |
| `pytsk3` | NTFS 볼륨 열거와 파일 읽기 | 아예 실행이 안 된다 |
| `libewf-python` | E01 열기, 분할 세그먼트 검증 | RAW 이미지만 분석할 수 있다 |
| `ccl-chromium-reader` | IndexedDB 정식 파싱 | 멈추진 않지만 대화의 발화자와 전송 시각을 복원하지 못한다 |
| `zstandard` | HTTP 캐시 응답 압축 해제 | 멈추진 않지만 캐시의 압축 구간 안을 못 본다 |
| `dissect.fve` | BitLocker 볼륨 해제 (`pycryptodome`도 같이 깔린다) | BitLocker 파티션만 `VOLUME_LOCKED`로 멈춘다. 일반 이미지와 라이브 입력은 그대로 된다 |

## 실행

```
python run_parser.py --input "D:\case\image.E01" --out-dir "D:\case\out" --format both --verbose
```

분할 E01은 첫 세그먼트(`.E01`)만 주면 나머지를 알아서 잇는다. 분할 RAW는 첫 조각(`.001`)을
준다. 형식은 확장자가 아니라 파일 시그니처로 판별한다.

| 옵션 | 역할 |
| --- | --- |
| `--input`, `-i` | E01(분할 포함)이나 RAW `.dd` `.raw` 이미지, 추출 디렉터리, 라이브면 볼륨 루트 |
| `--out-dir`, `-o` | 결과 폴더 (기본 `cl_out`) |
| `--format` | `csv` CSV 2종 (기본), `json` 전체 레코드와 근거, `both` |
| `--input-type` | `auto`(기본), `image`, `dir`, `live`. 라이브는 자동으로 잡히지 않으니 직접 줘야 한다 |
| `--key-material` | BitLocker 키. `recovery:<48자리>`, `passphrase:<암호>`, `bek:<.BEK 경로>`, `fvek:<hex>`. 환경변수 `ERELM_KEY_MATERIAL`로도 받는다 |
| `--kinds` | 파싱할 아티팩트 종류 제한 (쉼표 구분, 기본은 전체) |
| `--max-excerpt` | 근거 발췌 최대 길이 (기본 500자) |
| `--no-usn` | `$UsnJrnl:$J` 분석 생략 |
| `--usn-max-bytes` | `$J`에서 읽을 최대 바이트 |
| `--overwrite` | 기존 결과가 있어도 덮어쓴다 (기본은 거부) |
| `--verbose`, `-v` | 단계별 진행 상황 출력 |
| `--no-banner`, `--quiet` | 배너, 요약 생략 |

종료코드는 `0` 정상, `1` 파일 단위 오류 있음, `2` 입력 실패다.

덮어쓰기를 기본으로 막아둔 건 이전 실행 결과랑 새 결과가 한 폴더에 섞이는 걸 막으려는
것이다. 같은 폴더에 다시 내리려면 `--overwrite`를 준다.

### BitLocker 이미지

```
set ERELM_KEY_MATERIAL=recovery:123456-123456-123456-123456-123456-123456-123456-123456
python run_parser.py --input "D:\case\image.E01" --out-dir "D:\case\out"
```

파티션 선두의 `-FVE-FS-` 시그니처로 BitLocker를 알아보고 `dissect.fve`로 푼다. 푼 다음엔
복호화된 볼륨을 일반 NTFS와 똑같은 경로로 읽는다 (MFT, 삭제 엔트리, `$UsnJrnl`까지).

| 볼륨 상태 | 결과 |
| --- | --- |
| clear key(보호 일시 중단 등)나 복호화 완료 | 키 없이 열린다 |
| 복구 키, 암호, BEK, FVEK를 줌 | 그 키로 연다 |
| 키가 없거나 틀림 | `VOLUME_LOCKED` (종료코드 2). 볼륨의 키 보호자 목록을 같이 뿌린다 |
| 평문 볼륨 + 잠긴 볼륨이 섞임 | `VOLUME_LOCKED`를 내되, 복구 파티션 같은 평문 볼륨은 읽는다. "아티팩트 없음"으로 끝내버리지 않는다 |

키는 명령행 기록에 남지 않게 환경변수로 주는 걸 권한다. 키 값 자체는 콘솔이든 로그든
결과 파일이든 아무 데도 안 쓰고, 어떤 방법으로 풀었는지(`unlock_method`)만 `result.json`의
`filesystem.volumes`에 남긴다.

### 라이브 분석 (실행 중인 PC)

```
python run_parser.py --input-type live --out-dir "D:\out"
```

이미지를 안 뜨고 지금 이 PC의 `C:\`(시스템 드라이브)에서 Claude Desktop 저장 경로를 찾아
읽는다. 다른 드라이브나 마운트 폴더를 보려면 `--input D:\`처럼 준다.

이미지 분석과 같은 탐색 경로(설치 형태 세 가지에 앱 로그, 프리패치)만 본다. 디스크 전체를
훑지 않고, 판정 규칙도 이미지와 똑같다. 원본은 읽기 전용으로만 열고, 복사본이나 임시 파일을
원본 경로에 만들지 않는다. 출력 폴더가 탐색 경로 안에 있으면 실행을 거부한다.

실행 중인 앱이 잡고 있는 파일(HTTP 캐시 등)도 다른 프로세스와 공유하는 읽기로 연다.
그래도 못 읽은 파일은 `filesystem.read_failures`와 경고로 남는다. 관리자 권한이 아니면
`C:\Windows\Prefetch`와 `C:\Program Files\WindowsApps`를 못 보는데, 열지 못한 폴더는
`filesystem.denied`에 적힌다. 파일 시각과 MFT 번호는 `os.stat` 값이고 그 출처를 근거의
`time_source`에 적어 둔다. `Artifact_details`에는 `수집=live`가 붙는다.

## 출력

| 파일 | 내용 |
| --- | --- |
| `timeline.csv` | 판정된 사용자 행위 (확정, 의심만) |
| `recovered_conversations.csv` | 복원한 대화를 메시지 단위로 |
| `result.json` | 전체 레코드와 판정 근거 (`--format json`이나 `both`) |

CSV는 UTF-8 BOM으로 저장해서 엑셀에서 바로 열린다. 시각은 KST, 값이 없는 칸은 `-`로 둔다.

`timeline.csv`의 컬럼은 `Model`, `Timestamp`, `User_behavior`, `Behavior_details `,
`Verdict`, `Artifact_path`, `Artifact_details `다.

`Verdict`는 두 가지뿐이다. `확정`은 아티팩트가 그 행위를 직접 증명하는 경우, `의심`은 정황
증거만 있는 경우다. 근거를 못 찾은 행위는 행 자체를 만들지 않는다. `User_behavior`에는 고정된
행위 분류표의 이름만 쓴다 (`parser/events.py` 참고).

`Artifact_path`의 드라이브 문자는 가정이 아니라 실측이다. 이미지 입력이면
`HKLM\SYSTEM\MountedDevices`를 읽어서 각 NTFS 볼륨이 원래 PC에서 어떤 문자(`C:` 등)였는지
확인한다(`parser/mounted_devices.py`. MBR은 디스크 서명과 파티션 오프셋으로, GPT는 파티션
GUID로 대조한다). SYSTEM 하이브가 없거나 문자가 없는 파티션(복구, 예약)은 예전처럼 `C:`로
표기한다. 근거는 `result.json`의 `filesystem.drive_letters`에 남는다.

`Behavior_details `에는 `<행위>_<짧은 라벨>` 꼴 요약만 담는다. 로그 원문이나 타임스탬프 같은
긴 텍스트는 안 넣는다 (그건 `result.json`의 `details`와 `evidence.excerpt`에 있다). 팀 공통
양식에 맞춘 것이다.

앱 실행 한 번이 하위 프로세스마다 Prefetch를 남겨도 timeline에는 한 행으로 합친다. 가장 이른
것을 대표로 쓰고 라벨에 `prefetch_run=1/N`을 붙이며, 합쳐진 시각은 `Artifact_details `의 `병합=`과
`result.json`에 남긴다. 시간상 멀리 떨어진 실행은 각각 별개 행으로 둔다.

`recovered_conversations.csv`의 컬럼은 `Model`, `timestamp`, `conversation_uuid`,
`conversation_title`, `message_uuid`, `parent_message_uuid`, `message order`, `role`,
`content`, `Artifact_path`다.

`role`은 본문이 어디에서 나왔는지로 판정한 값이다.

| 값 | 뜻 |
| --- | --- |
| `user` | 사용자가 입력창에 친 말, 파일을 올린 말 |
| `assistant` | 모델 응답 |
| `system` | 앱이 대화에 끼워 넣은 안내 (`<system-reminder>` 등) |
| `tool` | 도구 실행 결과 |
| `unknown` | 역할 표식 없이 저장소에 남아 있던 본문 |

앱은 시스템 안내나 도구 결과도 발화자를 `user`로 적어 저장한다. 이걸 그대로 실으면 사용자가
한 말처럼 읽히니까 다시 판정한다.

`role=unknown`(발화자나 순서를 확인 못 한 바이트 스캔 조각)은 CSV에 안 싣는다. 팀 공통 양식이
발화자가 확인된 메시지만 담기 때문이다. 이 조각들은 `result.json`에만 남는다.

## 대화 복원

IndexedDB 값은 V8 구조화 복제로 저장된다. 이걸 제대로 역직렬화하면 발화자, 전송 시각, 대화
UUID, 부모 메시지가 기록된 값 그대로 나온다. Claude Desktop은 대화를 네 군데에 나눠 담는다.

| 데이터베이스 / 스토어 | 형태 | 얻는 것 |
| --- | --- | --- |
| `claude-conversation-store` / `trees` (`product=chat`) | `tree.chat_messages[]` | 발화자, 본문, 순번, 부모 메시지 |
| `claude-conversation-store` / `trees` (`product=cowork`) | `tree.events[].payload.message` | 역할, 본문, 세션 ID, 전송 시각 |
| `keyval-store` / `keyval` (`react-query-cache`) | 대화 목록 | 제목, 생성 시각, 모델, 임시 채팅 여부, 요약 |
| `claude-conversation-store` / `meta` | 열람 기록 | 마지막 열람 시각, 메시지 수 |

같은 부모 메시지 아래에 형제가 둘 이상이면 그 자리에서 대화가 갈라진 것이다. 사용자 메시지의
형제는 대화 수정으로, 모델 응답의 형제는 대화 재시도로 본다.

본문이 안 남은 대화는 목록의 `summary` 필드가 유일한 내용 단서라 같이 수집한다. 다만 이
요약은 앱이 만든 문장이지 사용자가 입력한 말이 아니다. 그래서 `recovered_conversations.csv`에는
안 싣고 `result.json`의 관찰 기록으로만 남기며, 근거 문구에 사용자 발화가 아니라고 적는다.

정식 파싱이 못 닿는 곳(압축 전 `.log`의 잔존분, 입력창 초안)만 원시 스캔으로 보완한다. 정식
파싱이 이미 읽은 저장소에서 원시 스캔이 다시 집어 올린 본문은 같은 대화의 잘린 조각이라
`result.json`에 근거로만 남기고 CSV에는 안 싣는다 (`superseded_by`).

## USN 저널 (`$UsnJrnl:$J`)

파일 자체가 안 남은 행위(지워진 첨부, 처음 실행, 설치와 삭제)의 시각을 보조로 잡을 때 쓴다.
USN 레코드에는 경로가 없고 이름과 부모 참조(MFT#, 순번)만 있어서, 아래 기준으로만 고른다.

| 고르는 기준 | 설명 |
| --- | --- |
| 부모가 Claude 탐색 폴더 | 이미지에서 훑은 Claude 폴더의 MFT 번호를 순번까지 맞춘다. MFT 엔트리가 재사용되면 순번이 달라지니 엉뚱한 파일을 Claude 것으로 잡지 않는다. 저널 기간에 그 아래 새로 생긴 폴더(첨부 저장소 등)도 따라간다. 프리패치나 WindowsApps 폴더 자체는 다른 프로그램 파일이 대부분이라 뺀다 |
| 이름만으로 Claude 것 | Claude 실행 파일 프리패치(`CLAUDE*.EXE-xxxxxxxx.pf`), WindowsApps의 Claude 패키지 폴더, Packages의 Claude 데이터 폴더 |

| USN 기록 | 판정 |
| --- | --- |
| Claude 실행 파일 프리패치 생성 | 애플리케이션 실행 의심. 단, 같은 기동의 Prefetch 실행 행이 이미 있으면 그 행의 근거로 붙이고 별도 행을 안 만든다(실행 1회가 두 행으로 나오지 않게). 프리패치 행이 없는 '첫 실행' 때만 독립 행이 된다 |
| Claude 설치 프로그램 프리패치 생성 | 애플리케이션 설치 의심 |
| WindowsApps Claude 패키지 폴더 생성 | 애플리케이션 설치 의심. 단, 앞선 설치 신호(SETUP 프리패치나 더 이른 패키지 폴더 생성)가 있으면 업데이트나 MSIX 스테이징으로 보고 관찰(NA-00, 후보 E02-01)로 내린다. 설치 행은 가장 이른 1건만 남긴다 |
| WindowsApps Claude 패키지 폴더 삭제 | 관찰(NA-00). 버전 업데이트도 옛 버전 폴더를 지우니 앱 삭제로 단정하지 않는다 |
| `Packages\Claude_pzs8sxrjxfjjc` 데이터 폴더 삭제 | 애플리케이션 삭제 의심 |
| IndexedDB 첨부 저장소(`*.indexeddb.blob`) 파일 생성/삭제 | 관찰(NA-00), 후보 E05-01/E05-03, E05-02/E05-04. 이미지인지 문서인지는 구분 못 한다 |
| 그 밖의 변경 | 폴더별 요약 관찰(NA-00) |

USN 시각은 저널에 기록된 시각이라 실제 행위 시각과 같다고 단정하지 않는다. 그래서 USN 행은
다른 저장소(앱 로그 등)에 의해 확정될 수는 있어도, 거꾸로 다른 행을 확정으로 올리는 근거로는
안 쓴다. 프리패치와 USN은 같은 앱 기동이 Prefetch 저장소 한 곳에 남긴 것이라(설령 `.pf`가
달라도) 서로 독립 근거로 치지 않고, 양쪽 모두 상대를 확정으로 올리지 않는다.

## 설정 파일 `claude_desktop_config.json`

MCP 서버(외부 도구 연결)와 앱 기본 설정이 들어 있다. MCP 서버는 이름이랑 실행 프로그램 이름만
남기고 인자나 환경변수(토큰, 경로) 값은 안 싣는다. 계정 UUID는 `preferences`의 `...ByAccount`
키에서만 읽어 근거로 남기고, 로그인 행은 만들지 않는다.

## 구조

```
run_parser.py          실행 진입점
parser/
  cli.py               인자 해석, 콘솔 UTF-8 설정, 덮어쓰기 보호
  pipeline.py          증거 열기 -> 경로 탐색 -> 파싱 -> 중복 정리 -> 교차검증
  image.py             E01(분할 검증 포함), RAW 열기
  vfs.py               NTFS 볼륨 열거와 파일 읽기 (삭제 엔트리 포함)
  locate.py            설치 형태별 경로 식별
  events.py            사용자 행위 분류표
  model.py             레코드 구조 (시각, 사용자 행위, 근거)
  timeline.py          시각순 정렬과 시각 미상 분리
  output.py            result.json, CSV 2종
  usn.py               $UsnJrnl:$J 해석
  util.py              시각 변환, 발췌, 민감정보 마스킹
  artifacts/
    idbtree.py         IndexedDB 정식 파싱 (V8 역직렬화)
    idbstore.py        파싱 결과를 레코드로 (발화자 판정, 분기 감지)
    idb.py             저장소 원시 스캔 (입력 초안, 잔존분)
    v8text.py          V8 문자열 이중 인코딩 해독
    misc.py            캐시, 설정 파일, 프리패치
    applog.py          Electron 로그, 타임존 보정
```
