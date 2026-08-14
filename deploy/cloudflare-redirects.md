# annodock.com 리다이렉트 규칙

상태: **사용자 Cloudflare Dashboard 적용 대기**

마케팅 랜딩이 생기기 전까지 아래 체인을 사용한다.

1. `www.annodock.com` → `https://annodock.com` (`301`)
2. `annodock.com` → `https://app.annodock.com` (`301`)

`annodock.app`은 구매하지 않았으므로 범위에 포함하지 않는다. 이 문서는 적용값과
검증 절차만 제공하며 Cloudflare 설정을 직접 변경하지 않는다.

## 현재 외부 읽기 결과

2026-08-10 12:46 KST에 인증정보 없이 GET과 HEAD를 다시 확인했다.

| 요청 | GET | HEAD | `Location` | 판정 |
| --- | ---: | ---: | --- | --- |
| `https://www.annodock.com/phase7-check?source=www` | `404` | `404` | 없음 | www 규칙 미적용 |
| `https://annodock.com/phase7-check?source=apex` | `404` | `404` | 없음 | apex 규칙 미적용 |
| `https://app.annodock.com/` | `200` | `200` | 해당 없음 | 최종 서비스 정상 |

www와 apex 응답의 `server`는 모두 `cloudflare`였다. 따라서 DNS/TLS 도달 실패가
아니라 Cloudflare edge에서 Redirect Rule이 아직 요청을 종료하지 않는 상태다.

## DNS 전제

Single Redirects는 대상 호스트의 트래픽이 Cloudflare에서 **Proxied** 상태여야 한다.
현재 배포에서는 `@`·`www`·`app`의 Tunnel-managed DNS route를 유지한다. 리다이렉트를
위해 이 레코드를 일반 A 레코드로 바꾸거나 Tunnel CNAME을 삭제하지 않는다.

Cloudflare 공식 문서:

- <https://developers.cloudflare.com/rules/url-forwarding/single-redirects/create-dashboard/>
- <https://developers.cloudflare.com/rules/url-forwarding/single-redirects/settings/>
- <https://developers.cloudflare.com/rules/url-forwarding/examples/redirect-all-different-hostname/>

## Dashboard 적용 절차

Cloudflare Dashboard에서 `annodock.com` zone을 선택하고 **Rules → Overview →
Create rule → Redirect Rule**로 이동한다. 각 규칙은 **Custom filter expression**을
사용한다.

### 규칙 1: www → apex

- Rule name: `annodock-www-to-apex`
- When incoming requests match → Custom filter expression:
  `(http.host eq "www.annodock.com")`
- Then → Type: `Dynamic`
- Expression:
  `concat("https://annodock.com", http.request.uri.path)`
- Status code: `301 - Permanent Redirect`
- Preserve query string: `Enabled`
- **Deploy** 선택

### 규칙 2: apex → app

- Rule name: `annodock-apex-to-app`
- When incoming requests match → Custom filter expression:
  `(http.host eq "annodock.com")`
- Then → Type: `Dynamic`
- Expression:
  `concat("https://app.annodock.com", http.request.uri.path)`
- Status code: `301 - Permanent Redirect`
- Preserve query string: `Enabled`
- **Deploy** 선택

경로는 Dynamic expression이 유지하고 query string은 별도 옵션이 유지한다. 대상
호스트 `app.annodock.com`은 어느 조건에도 일치하지 않으므로 루프가 생기지 않는다.

## 적용 직후 검증

다음 읽기 전용 명령으로 path와 query까지 확인한다.

```bash
curl -sS --max-redirs 0 -o /dev/null \
  -w 'status=%{http_code} location=%{redirect_url}\n' \
  'https://www.annodock.com/phase7-check?source=www'

curl -sS --max-redirs 0 -o /dev/null \
  -w 'status=%{http_code} location=%{redirect_url}\n' \
  'https://annodock.com/phase7-check?source=apex'

curl -sS --max-redirs 5 -o /dev/null \
  -w 'status=%{http_code} final=%{url_effective} redirects=%{num_redirects}\n' \
  'https://www.annodock.com/phase7-check?source=www'
```

합격값은 다음과 같다.

- 첫 요청: `301`, Location =
  `https://annodock.com/phase7-check?source=www`
- 두 번째 요청: `301`, Location =
  `https://app.annodock.com/phase7-check?source=apex`
- follow 요청: 최종 `200`, 최종 host = `app.annodock.com`, redirect 횟수 = `2`

`404`, Location 없음, redirect 횟수 `0` 중 하나라도 나오면 아직 미적용이다.

## 전환과 되돌리기

마케팅 랜딩을 apex에 배포할 때는 **규칙 2(apex → app)만 비활성화**한다. 규칙
1(www → apex)은 canonical host 정규화를 위해 유지한다. 잘못 적용했을 때도 DNS route를
삭제하지 말고 해당 Redirect Rule만 비활성화하면 된다.
