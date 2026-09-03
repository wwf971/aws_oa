// token storage shared by the login page and the main page (localStorage,
// so both pages of the same origin see the same login state).

const STORAGE_KEY = 'asset-service-tokens'
const EXPIRE_MARGIN_MS = 60 * 1000

export function tokensSave(tokenResponse, refreshTokenPrev) {
  const tokens = {
    accessToken: tokenResponse.access_token,
    idToken: tokenResponse.id_token,
    // the refresh_token grant response has no refresh_token, keep the old one
    refreshToken: tokenResponse.refresh_token || refreshTokenPrev || null,
    expiresAt: Date.now() + tokenResponse.expires_in * 1000,
  }
  localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
  return tokens
}

export function tokensLoad() {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

export function tokensClear() {
  localStorage.removeItem(STORAGE_KEY)
}

export function getIsTokensExpired(tokens) {
  return Date.now() > tokens.expiresAt - EXPIRE_MARGIN_MS
}

export function jwtClaimsParse(token) {
  const payload = token.split('.')[1]
  const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'))
  return JSON.parse(json)
}
