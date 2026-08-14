# Annodock 운영 OAuth 설정

운영 브라우저 오리진은 `https://app.annodock.com` 하나다. 개발용 OAuth 앱과
운영용 OAuth 앱/클라이언트는 분리하고, 아래 값은 운영 앱에만 등록한다.

## 확인된 콜백 계약

2026-08-06에 실제 `modules/auth-service/app/routes/oauth.py`의
`_callback_uri()`와 `_success_redirect()`를 운영 환경값으로 격리 실행하고,
실행 중 auth 컨테이너의 Google/Kakao/Naver authorize 302에서도
`redirect_uri`만 추출해 다음 결과가 동일함을 확인했다.

| 제공자 | 제공자 콘솔에 등록할 Redirect URI |
| --- | --- |
| Google | `https://app.annodock.com/auth/oauth/google/callback` |
| Kakao | `https://app.annodock.com/auth/oauth/kakao/callback` |
| Naver | `https://app.annodock.com/auth/oauth/naver/callback` |

인증 완료 후 auth-service가 SPA로 보내는 내부 최종 주소는
`https://app.annodock.com/auth/callback`이다. 이 주소는 제공자 콘솔이 아니라
`ALLOWED_REDIRECT_URIS`에 들어간다.

컨테이너 재적용 후에는 콘솔 값을 저장하기 전에 반드시 실제 authorize 302를 다시
측정한다. 아래 명령은 `Location` 전체의 state/client 정보를 출력하지 않고
`redirect_uri`만 출력한다.

```bash
AUTH_PORT=$(awk -F= '$1 == "AUTH_PORT" {print $2; exit}' .env)
for provider in google kakao naver; do
  curl -sS -D - -o /dev/null \
    "http://localhost:${AUTH_PORT}/auth/oauth/${provider}/authorize" \
    | awk 'BEGIN{IGNORECASE=1} /^location:/{sub(/^[^:]+:[[:space:]]*/, ""); sub(/\r$/, ""); print}' \
    | python3 -c 'import sys, urllib.parse as u; print(u.parse_qs(u.urlsplit(sys.stdin.read().strip()).query)["redirect_uri"][0])'
done
```

세 줄이 위 표와 바이트 단위로 같지 않으면 콘솔 등록을 중단하고
`OAUTH_REDIRECT_BASE` 주입부터 고친다. 스킴, 호스트, 경로와 끝 슬래시는 모두
일치해야 한다.

## Google

1. Google Cloud Console의 **Google Auth Platform → Clients**로 이동한다.
2. 운영 전용 **Web application** 클라이언트를 만든다.
3. Authorized JavaScript origins에 `https://app.annodock.com`을 등록한다.
4. Authorized redirect URIs에 표의 Google URI 한 개를 등록한다.
5. 발급된 client ID/secret은 Git에 넣지 않고 auth-service 런타임 환경에만 넣는다.
6. 운영 공개 전 홈페이지, 개인정보처리방침, 이용약관 URL과 동의 화면을 완성한다.

공식 문서: <https://developers.google.com/identity/protocols/oauth2/web-server>

## Kakao

1. Kakao Developers의 **내 애플리케이션**에서 운영 앱을 선택하거나 새로 만든다.
2. **제품 설정 → 카카오 로그인**을 활성화한다.
3. Redirect URI에 표의 Kakao URI를 정확히 등록한다.
4. REST API 키와 필요한 client secret을 auth-service 런타임 환경에만 넣는다.
5. 필요한 동의 항목과 비즈니스 검수 상태를 확인한다.

공식 문서: <https://developers.kakao.com/docs/ko/kakaologin/faq>

## Naver

1. Naver Developers의 **Application → 애플리케이션 등록/수정**으로 이동한다.
2. 운영용 애플리케이션의 PC 웹 서비스 URL을 `https://app.annodock.com`으로 둔다.
3. Callback URL에 표의 Naver URI를 정확히 등록한다.
4. 운영 client ID/secret을 auth-service 런타임 환경에만 넣는다.
5. 제공 정보와 서비스 적용 상태를 확인한다.

공식 문서: <https://developers.naver.com/docs/login/api/>

## 비밀값 취급

- client secret, Cloudflare 자격증명, OAuth state를 문서·로그·Git에 기록하지 않는다.
- 운영 키를 개발용 콜백 allowlist에 재사용하지 않는다.
- 키가 노출되면 해당 제공자 콘솔에서 즉시 재발급하고 기존 키를 폐기한다.
