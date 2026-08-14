# Annodock 외부 실사용 E2E 체크리스트

상태: **외부 읽기 검증 일부 완료 · Cloudflare 리다이렉트와 사용자 브라우저 게이트 대기**

민감정보·실사용자 데이터는 이 파일에 기록하지 않는다. 아래의 자동 읽기 검증은
게이트3 사용자 실사용 확인을 대체하지 않는다.

## 1. 읽기 전용 검증 기록

- 검증 일시: 2026-08-10 12:43~12:46 KST
- 방식: 인증정보 없는 외부 GET/HEAD와 로컬 compose 정적 해석
- 변경 작업: Cloudflare write 없음, 컨테이너·systemd 재시작 없음, 사용자 데이터 접근 없음

### 외부 인그레스

| 결과 | 항목 | 실제 응답 |
| --- | --- | --- |
| PASS | `https://app.annodock.com/` TLS/SPA | GET·HEAD `200`, TLS 검증 `0` |
| PASS | `/api/health` | `200` |
| PASS | 무인증 `/api/datasets` | `401` |
| PASS | `/auth/health` | `200` |
| PASS | `/reset?token=dummy` | `200`, SPA 반환 |
| PASS | `/auth/callback` | `200`, SPA 반환 |
| PENDING | www → apex | GET·HEAD `404`, Location 없음 |
| PENDING | apex → app | GET·HEAD `404`, Location 없음 |

리다이렉트 적용값과 검증 명령은 `deploy/cloudflare-redirects.md`에 기록했다.

### 재시작·키 회수 안전 배선

- [x] `docker-compose.auth.yml`의 `auth`와 `mailhog` 모두
  `restart: unless-stopped`다.
- [x] OAuth provider ID/secret 6개를 빈 값으로 덮어도
  `docker compose -f docker-compose.auth.yml config -q`가 통과한다.
- [x] 실제 `./dev.sh up`은 OAuth 6개 중 하나라도 없으면 Docker 호출 전에
  fail-closed한다. 빈 provider 설정으로 새 auth 컨테이너를 기동하지 않는다.
- [x] CTO 사전 읽기 실측에서 proxy/tunnel systemd user unit은 active·enabled,
  linger는 enabled, auth 컨테이너는 running이며 restart policy는
  `unless-stopped`였다.
- [ ] **미검증:** 실제 호스트 재부팅 또는 auth 컨테이너 재시작 뒤 자동 복구.

OAuth 키의 compose-time `${VAR:?}` 의존을 제거했기 때문에 키 회수 중에도 compose
파일 파싱이 먼저 막히지 않는다. `PUBLIC_APP_URL`과 `AUTH_PORT`는 자격증명이 아니라
서비스 주소 계약이며, 잘못된 콜백 또는 임의 포트 기동을 막기 위해 필수값으로 유지한다.
Docker daemon은 이미 만들어진 컨테이너의 저장된 `unless-stopped` 정책으로 재부팅
복구하므로 OAuth 키 파일을 다시 읽어 compose 파싱할 필요가 없다.

## 2. 사용자 브라우저 실사용 게이트

다음 항목은 실제 외부 네트워크의 브라우저에서 사용자가 확인한 뒤에만 체크한다.

- 검증 일시:
- 실행자:
- 브라우저 및 외부 네트워크:

### 계정과 OAuth

- [ ] 신규 테스트 계정을 가입하고 로그인한다.
- [ ] 계정 메뉴에서 로그아웃한 뒤 다시 로그인한다.
- [ ] 비밀번호 재설정 메일 링크가 열리고 변경 성공 뒤 로그인 화면으로 자동 이동한다.
- [ ] Google 로그인이 authorize → callback → exchange까지 왕복한다.
- [ ] Kakao 로그인이 authorize → callback → exchange까지 왕복한다.
- [ ] Naver 로그인이 authorize → callback → exchange까지 왕복한다.

### 데이터셋·라벨링·학습

- [ ] 테스트 프로젝트를 만들고 zip 데이터셋을 그 프로젝트 안에 업로드한다.
- [ ] 인제스트 이슈 리포트를 확인한다.
- [ ] bbox를 생성·수정·삭제하고 새로고침 후 자동저장 결과를 확인한다.
- [ ] YOLO export를 내려받아 라벨 왕복 결과를 확인한다.
- [ ] 학습을 제출하고 진행률·차트·로그를 확인한다.
- [ ] 완료 후 `best.pt`를 내려받는다.

### 사용자 격리와 쿼터

- [ ] 두 번째 테스트 사용자로 첫 사용자의 project·dataset 상세 접근 시 `404`를 받는다.
- [ ] 두 번째 테스트 사용자로 첫 사용자의 run 상세·artifact 접근 시 각각 `404`를 받는다.
- [ ] 격리된 seedqa 계정으로만 쿼터를 소진해 업로드·학습·export가 모두 `413`인지 확인한다.

실사용자 프로젝트나 데이터셋을 쿼터·삭제·격리 검증에 사용하지 않는다.

### 외부 도메인과 재부팅 복구

- [ ] `www.annodock.com`이 path/query를 유지해 apex로 `301`을 반환한다.
- [ ] apex가 path/query를 유지해 app으로 `301`을 반환한다.
- [ ] www → apex → app 체인에 루프가 없고 최종 SPA가 `200`이다.
- [ ] 승인된 점검 창에서 호스트를 재부팅한 뒤 proxy·tunnel·auth가 자동 복구된다.
- [ ] 복구 후 가입·로그인과 무인증 API `401`을 다시 확인한다.
- [ ] 로그와 Git 추적 파일에 Tunnel/OAuth 자격증명이 없다.

## 3. 최종 결과

- [ ] 사용자 브라우저 게이트 전 항목 PASS
- 실패 항목 및 재현 절차:
- 증거(민감정보 제거):

현재 판정: **미완료**. 기계적 외부 도달성은 확인됐지만 리다이렉트 규칙 적용과
사용자 브라우저 여정, 실제 재부팅 자동 복구는 아직 확인되지 않았다.
