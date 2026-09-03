// oauth2 authorization code + pkce against the cognito hosted endpoints.
// see asset_service_impl.md#login-flow-oauth2-authorization-code--pkce

const PKCE_VERIFIER_KEY = 'asset-service-pkce-verifier'
const PKCE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'

function pkceVerifierCreate() {
  const bytes = new Uint8Array(64)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (byte) => PKCE_CHARS[byte % PKCE_CHARS.length]).join('')
}

async function pkceChallengeCompute(verifier) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  const base64 = btoa(String.fromCharCode(...new Uint8Array(digest)))
  return base64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function redirectUriBuild() {
  // the login page itself is the oauth callback; must exactly match a
  // callback url registered on the cognito app client
  return `${window.location.origin}/`
}

export async function authorizeRedirect(webConfig) {
  const verifier = pkceVerifierCreate()
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier)
  const challenge = await pkceChallengeCompute(verifier)
  const params = new URLSearchParams({
    client_id: webConfig.cognito.client_id,
    response_type: 'code',
    scope: 'openid email',
    redirect_uri: redirectUriBuild(),
    code_challenge_method: 'S256',
    code_challenge: challenge,
  })
  window.location.href = `${webConfig.cognito.domain}/oauth2/authorize?${params.toString()}`
}

async function tokenEndpointCall(webConfig, form) {
  const resp = await fetch(`${webConfig.cognito.domain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(form).toString(),
  })
  const body = await resp.json()
  if (!resp.ok) {
    throw new Error(`token endpoint error: ${body.error || resp.status}`)
  }
  return body
}

export async function codeExchange(webConfig, code) {
  const verifier = sessionStorage.getItem(PKCE_VERIFIER_KEY)
  if (!verifier) {
    throw new Error('pkce verifier missing (login was not started from this page)')
  }
  const tokenResponse = await tokenEndpointCall(webConfig, {
    grant_type: 'authorization_code',
    client_id: webConfig.cognito.client_id,
    code,
    redirect_uri: redirectUriBuild(),
    code_verifier: verifier,
  })
  sessionStorage.removeItem(PKCE_VERIFIER_KEY)
  return tokenResponse
}

export async function tokenRefresh(webConfig, refreshToken) {
  return tokenEndpointCall(webConfig, {
    grant_type: 'refresh_token',
    client_id: webConfig.cognito.client_id,
    refresh_token: refreshToken,
  })
}
