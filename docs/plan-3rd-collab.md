# plan.md 3차 초안 — 협업 라벨링 (CEO 반영용)

> CTO 초안 2026-10-13. 사용자 확정 결정 10개 + Codex developer 토의(#1285→#1286) 반영.
> 분해 전 결정(A~E)은 같은 날 사용자가 전부 확정했다 — CEO 가 추가로 물을 것 없음.
> CEO 는 이 문서의 §4-3 을 `plan.md` §4 뒤에, §5-3·§6-3 을 §5·§6 에 **append** 한다
> (재번호 금지). Build Type 은 extend 축 → `specs/tasks.md` 에 `phase18~` 로 이어붙인다.
> 3차-B(데이터셋 판매·결제·마켓)는 **별도 발주** — 이 문서 범위 아님.

## 4-3. Requirements — 3차 (협업 라벨링)

**목표**: 한 팀이 한 데이터셋을 **나눠 달 수 있게** 한다 — 공유 + 배정 + 진행률.
성공 기준 = 로드맵 KPI "협업 프로젝트 30+" (~27.05). 비목표(이번 범위 아님):
검수 승인 워크플로(상태값만 확장 가능하게), 실시간 공동 편집·프레즌스, 외주·
크라우드 배분, 판매·결제.

**사용자 확정 결정 (2026-10-13)**
1. 협업 단위 = **팀 워크스페이스가 소유** (슬랙/노션 모델).
2. 개인도 워크스페이스 — 가입 시 1인 personal 자동 생성, 소유 주체는 한 종류.
3. 배정 = **자기 선점 + 자동 분배**.
4. 쿼터 = 워크스페이스마다 무료 캡 + 사용자당 팀 생성 상한.
5. 구현 = `owner_id` → `workspace_id` 전면 치환 + `X-Workspace-Id` 헤더 선택.
6. "라벨 완료" = 라벨러의 **완료 버튼** (명시적). 자동저장은 완료가 아니다.
7. 계정 삭제 시 팀 데이터 **존속**, 마지막 owner 는 삭제 거부(위임 선행).
8. 전환 = **무작업 창**(활성 run 0·워커 종료, 수분 다운타임). old/new SQL 호환층 없음.
9. 프로젝트 이전 시 원 워크스페이스에 공유 참조가 남는 미디어는 **거부**(분리 복사 없음).
10. 순서 = **3차 먼저**, 2차(폴리곤·키포인트)는 뒤로.

### 요구사항

- **워크스페이스 소유 모델 (전면 치환)** — `projects`·`datasets`·`media_objects`·
  `training_runs` 의 `owner_id` → `workspace_id`, `user_storage` → `workspace_storage`.
  `workspaces(id, kind personal|team, name, created_by, created_at)`,
  `workspace_members(workspace_id, user_id, role)`, `workspace_invites`.
  - **actor ≠ scope**: `CurrentUser.id` 는 계속 **사용자**(관리자 권한·행위자 기록).
    워크스페이스는 별도 의존성 `CurrentWorkspace` 가 해석한다 — 헤더 있으면 멤버십
    검증, 없으면 personal. 리소스 id 로 들어오는 경로(`/api/images/{id}/…`, run
    artifacts, 다운로드)는 **리소스에서 워크스페이스를 역조회**해 멤버십·역할 검사.
    헤더와 리소스 불일치 = 404(기존 "스코프 밖 404" 계약 유지), 역할 부족 = 403(신규).
  - **워커·콜백**: 제출 시점에 run 에 `workspace_id` 와 actor(`created_by`)를 고정.
    `train_worker`·`callbacks`·`failure`·`quota` sync·CLI `--owner-id` 의 직접 SQL
    전부 같은 의미로 전환.
  - `admin_users` 는 사용자 키 유지(플랫폼 관리자 ≠ 워크스페이스 역할). F13 골격
    `orgs`·`teams`·`memberships` 는 DB 비어있음 확인 후 삭제(FK 순서·fixture 갱신).
  - **마이그레이션**: 귀속 집합 = 위 5개 테이블 `owner_id` 합집합(bootstrap owner=1
    포함, auth 사용자와 대조). personal 워크스페이스를 **id = 기존 owner_id** 로 지정
    생성(백필 단순화 목적 — 디스크 경로는 `datasets/<id>`·`uploads/<id>`·
    `training-runs/<id>` 로 리소스 id 기반이라 경로 보존과 무관). 시퀀스
    `setval(max, true)`, 빈 테이블 분기. `quota_limit_bytes` override 보존. 데이터
    없는 사용자는 첫 요청에 지연 생성(유니크 + ON CONFLICT 로 동시 생성 경쟁 처리).
    **personal 조회는 멤버십으로** — `workspace.id == user.id` 가정 금지.
    downgrade = personal→사용자 역매핑, team 데이터 있으면 거부.
  - **분리 워커 호환**: 워커는 API 재시작으로 교체되지 않는다 → 컬럼 개명 직후 old
    워커 SQL 이 깨진다. 전환은 **활성 run 0 + 워커 전원 종료 상태**에서만 적용
    (결정 8 — 호환층 없음).

- **역할·권한 매트릭스** — 멤버십 존재만으로 쓰기 허용 금지. 전 mutation API 에 역할
  검사.

  | 동작 | viewer | editor | admin | owner |
  |---|---|---|---|---|
  | 보기·다운로드 | ✓ | ✓ | ✓ | ✓ |
  | 라벨·선점·완료 | | ✓ | ✓ | ✓ |
  | 업로드(워크스페이스 쿼터 소비) | | ✓ | ✓ | ✓ |
  | 클래스 관리·데이터셋 이름/삭제·병합 | | | ✓ | ✓ |
  | 학습 실행·run 삭제 (GPU 1장) | | | ✓ | ✓ |
  | 균등 배정·회수·재배정 | | | ✓ | ✓ |
  | 멤버·초대·쿼터 override | | | ✓ | ✓ |
  | 소유권 위임·워크스페이스 삭제·프로젝트 이전 | | | | ✓ |

  owner 는 정확히 1명, **0명 불가**(위임 후에만 탈퇴·삭제). 단일 GPU 동시 1 run 제한은
  **전역 유지**(워크스페이스별 아님).

- **멤버·초대** — 초대는 **링크 공유**로만 한다(이메일 초대 없음 — 2026-10-13 사용자
  결정). admin+ 가 역할 고정 초대 링크(만료 기본 7일·선택적 사용 횟수·철회·활성 목록)를
  만들고, 받은 사람은 annodock 에서 로그인(또는 가입) 뒤 "<팀> 워크스페이스에
  들어가시겠어요?" 확인 한 번으로 링크 역할의 멤버가 된다. 이메일 지정·이메일 매칭·auth
  DB 조회 없음 — 링크 소지가 자격이고 유출 위험은 만료·사용 횟수·철회로 통제. 역할은 admin
  이하만. 탈퇴·제거 시 그 사람의 `in_progress` 배정은 반환(assignee 해제), `labeled_by`
  이력은 보존. 팀 생성 상한 env `TEAM_CREATE_LIMIT`(기본 2), `created_by` 기준 — 위임
  뒤에도 생성자 계수 유지, 생성은 상한 검사와 원자적.
  - **계정 삭제 계약** (결정 7): 현행 관리자 삭제는 사용자 데이터 전부 +
    auth DB 행을 직접 지운다. 3차부터: personal 워크스페이스만 삭제, team 멤버십
    제거, **team 데이터 존속**, 마지막 owner 면 삭제 거부(위임 선행). 두 DB 삭제는
    단일 트랜잭션이 아니므로 순서(annodock 먼저 → auth) 와 재시도 명시.

- **배정·선점·진행률** — `images` 에 `label_status(unlabeled|in_progress|labeled)`,
  `assignee_id`, `assigned_at`, `labeled_by`, `labeled_at`, `version`. 인덱스
  `(dataset_id, label_status, assignee_id)`.
  - 선점 = 미배정·unlabeled 1장을 `FOR UPDATE OF images SKIP LOCKED` 로 고르고 **같은
    트랜잭션**에서 assignee·`in_progress` 갱신(권한·스코프 조건 포함). 빈 결과 ≠ 완료
    — 후보가 잠겨 있을 수 있으니 "지금 가져올 이미지 없음, 다시 시도" 로 표시.
  - 균등 배정(admin+) = 미배정만 대상, 진행 중 배정 덮지 않음, 선점과 **같은 후보
    조건**. 회수·재배정 admin+. 자동 만료 없음(v1) — 관리자 회수로.
  - 진행률 = 컬럼 집계(데이터셋별·멤버별), 별도 테이블 없음.
  - **완료 = 완료 버튼** (결정 6): 라벨러가 누르면 `labeled` + `labeled_by`=actor +
    `labeled_at`. 자동저장은 `in_progress` 유지. "객체 없음" 도 완료 버튼으로 확정
    (빈 라벨 + labeled). 완료 취소 = 본인 또는 admin+ → `in_progress`. 백필: 가져온
    라벨(`has_label_source` 또는 박스 있음) = `labeled`, **`labeled_by` 는 NULL**(가짜
    작업자 이력 금지). `is_modified` 는 "앱에서 수정됨" 의미 그대로.

- **동시 편집 충돌 (낙관적 잠금)** — 남의 `in_progress` 이미지는 읽기 전용(admin
  예외). GET/PUT 응답에 `version`, PUT 에 `expected_version`; 기존 Image+Dataset
  `FOR UPDATE` 잠금 **안에서** 비교 → 전체 교체 → +1. 불일치 = 409
  `{code: version_conflict, current}`. **클래스 재정렬(remap)도 해당 데이터셋 전
  이미지 version +1**(카탈로그 세대 — id 0↔1 교체는 존재 검증으로 못 잡는다).
  Dataset 잠금은 학습 snapshot 정합 경계라 유지.
  - Viewer: 저장 성공 응답의 version 을 다음 대기 편집에 이어 붙이고, 409 는 최신
    라벨 재조회 + "내 것 유지 / 서버 것으로" 선택 — 현재 4xx→rejected 경로는 재조회가
    없어 충돌 해결이 아니다. 백엔드와 Viewer 는 **같은 세그먼트**에 공개하고, 그 전엔
    `expected_version` 을 선택값으로 둔다(기존 PUT 안 깨지게).

- **쿼터 — 워크스페이스 단위** — `workspace_storage(bytes_used, quota_limit_bytes)`.
  increase/decrease 호출 지점 전부 키 전환(ingest·extract·merge·training artifact·
  cleanup·삭제 확정·실패·취소), **잠금 순서 보존**(Dataset/Project/Job → storage —
  새 잠금을 진입점 맨 앞에 두지 않는다). team 무료 캡 = personal 과 동일 env.
  preflight 는 예약 없음(동시 통과 가능) — 기존과 동일, 변경 없음을 명시.

- **프로젝트 이전 (개인 ↔ 팀)** — **소유권 이동, 복사 아님**: project + datasets +
  images + annotations + media_objects + runs/artifacts + exports 를 한 단위로
  `workspace_id` 변경. 미디어 공유 금지 전제라 이전 대상 미디어가 원 워크스페이스의
  다른 데이터셋에서도 참조되면 **거부**(결정 9). 회계 = MediaObject 단위 생존
  참조 기준 물리 바이트를 원→대상 이동(이미지 바이트 합산 금지 — 이중 계상),
  대상 쿼터 사전 검사, 양쪽 카운터·귀속 원자적, 진행 중 업로드/학습 있으면 거부.
  `project_merge` 재사용 불가(동일 소유자 내 병합용).

- **프런트** — AppShell 워크스페이스 스위처; API 클라이언트 헤더 주입(401 재시도는
  원 요청의 워크스페이스 고정; `/auth` 는 별도 경로); 전환 시 캐시 무효화 4종
  (이미지 blob·annotation·quota·업로드 resume) + 진행 중 저장/업로드는 원 스코프 고정;
  `?ws=` 를 링크 생성 지점(navigation·upload·Viewer)에서 전파, 열 때 멤버십 재확인;
  멤버 페이지(목록·역할·초대·대기 초대·탈퇴); 데이터셋 페이지 진행률(멤버별)·균등
  배정; Viewer "다음 이미지" 선점·배정 배지·읽기전용 표시·409 복구 UI; 프로젝트 이전
  다이얼로그. 디자인은 designer-cto — `/decompose --design` 여부는 CEO 판단.

- **관리자 대시보드 적응** — 사용자별 사용량 → 사용자가 속한 워크스페이스별 사용량,
  quota override 는 워크스페이스 단위, 계정 삭제는 위 계약대로.

### 분해 가이드 (세그먼트 순서)

1. **스키마 + 모델 + `CurrentWorkspace` + 호환 전환** — 라우터·서비스·워커 SQL·CLI 를
   **같은 세그먼트**에서 전환, 헤더 없는 요청 = personal. 목표: 기존 사용자 체감 변화
   0. 게이트 = 기존 전 테스트 + 회귀 게이트 0 + 수동 시나리오(업로드 재개·학습·관리자).
2. 역할 매트릭스 + 계정 삭제/탈퇴 계약 + 워크스페이스 CRUD·멤버·초대 API.
3. 배정·선점·version — 백엔드와 Viewer 같은 세그먼트.
4. 프런트 스위처·멤버 페이지·진행률·`?ws`·캐시 무효화.
5. 프로젝트 이전.
6. 관리자 대시보드 적응.

## 5-3. Constraints (3차 추가)

- **전환 창**: 세그먼트 1 의 마이그레이션은 활성 run 0 · 워커 종료 · 진행 중 업로드 0
  상태에서 사용자 승인 하에 적용. prod 가 작업 트리를 직접 서빙(리로드 = 배포)하므로
  off-hours. 마이그레이션 번호 `0021~`.
- **auth-service read-only 유지** — 단 계정 삭제의 auth DB 직접 삭제는 현행대로(결정 7).
- 테스트 DB `annodock_test` 직렬 실행(회귀 게이트 pre-flight 가 강제).
- 단일 GPU 동시 1 run 전역 제한 유지. 대기열은 여전히 non-goal.
- 원본 모듈 3개 read-only.

## 사용자 확정 (2026-10-13 — 분해 전 결정 완료)

- **A. 라벨 완료 정의** → 완료 버튼(명시). 가져온 라벨은 `labeled`, `labeled_by` NULL. (결정 6)
- **B. 계정 삭제 시 팀 데이터** → 존속, 마지막 owner 삭제 거부. (결정 7)
- **C. 전환 방식** → 무작업 창. 호환층 없음. (결정 8)
- **D. 이전 시 공유 미디어** → 거부. (결정 9)
- **E. 순서** → 3차 먼저. (결정 10)

## 6-3. Open Questions (CTO 분해 때 해소)

- `?ws=` 와 헤더가 다를 때 우선순위·전환 UX.
- 초대 메일 발송 수단(현 SMTP 상태) — 없으면 "대기 초대 + 링크 복사" 로 시작.
- 대기 초대 매칭 시점 = annodock 측 `CurrentUser` 생성 시점(로그인 훅 위치).
- 이미지 blob·annotation·quota·resume 캐시 키에 워크스페이스를 넣는 방식.
- `cleanup` 보존 정책(개수·일수)의 워크스페이스 단위 전환 수치.
- 관리자 대시보드 집계 쿼리(사용자↔워크스페이스 조인) 비용.
- `exports` 모델 잔존 참조 정리(현 HEAD 에 export route 없음).
- 선점 후보 정렬(id 순 vs 업로드 순)과 split 별 선점 여부.
