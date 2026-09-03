// auth state of the main page: tokens, current user info (from /api/me,
// so the role is decided by the server), api call helper with token refresh.
import { makeAutoObservable, runInAction } from 'mobx'
import { webConfigLoad } from '../../shared/webConfig.js'
import { tokensSave, tokensLoad, tokensClear, getIsTokensExpired } from '../../shared/token.js'
import { tokenRefresh } from '../../shared/authApi.js'

class AuthStore {
  webConfig = null
  tokens = null
  me = null // { user_id, username, email, role }
  initState = 'pending' // pending | ready | no-access | error
  initErrorText = ''

  constructor() {
    makeAutoObservable(this, {}, { autoBind: true })
  }

  get isAdmin() {
    return this.me?.role === 'admin'
  }

  async init() {
    try {
      const webConfig = await webConfigLoad()
      runInAction(() => {
        this.webConfig = webConfig
      })

      const tokens = tokensLoad()
      if (!tokens) {
        this.redirectToLogin()
        return
      }
      runInAction(() => {
        this.tokens = tokens
      })

      const respMe = await this.apiCall('GET', '/me')
      if (respMe.code === 0) {
        runInAction(() => {
          this.me = respMe.data
          this.initState = 'ready'
        })
      } else {
        runInAction(() => {
          this.initState = 'no-access'
          this.initErrorText = respMe.message
        })
      }
    } catch (error) {
      runInAction(() => {
        this.initState = 'error'
        this.initErrorText = String(error.message || error)
      })
    }
  }

  redirectToLogin() {
    tokensClear()
    window.location.replace('/')
  }

  logout() {
    this.redirectToLogin()
  }

  async accessTokenGet() {
    if (!this.tokens) {
      this.redirectToLogin()
      return null
    }
    if (!getIsTokensExpired(this.tokens)) {
      return this.tokens.accessToken
    }
    if (!this.tokens.refreshToken) {
      this.redirectToLogin()
      return null
    }
    try {
      const tokenResponse = await tokenRefresh(this.webConfig, this.tokens.refreshToken)
      const tokens = tokensSave(tokenResponse, this.tokens.refreshToken)
      runInAction(() => {
        this.tokens = tokens
      })
      return tokens.accessToken
    } catch {
      this.redirectToLogin()
      return null
    }
  }

  // all api responses are {code, data, message}; code 0 = success
  async apiCall(method, path, body) {
    const token = await this.accessTokenGet()
    if (!token) {
      return { code: -10, data: null, message: 'not logged in' }
    }
    const headers = { Authorization: `Bearer ${token}` }
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json'
    }
    const resp = await fetch(this.webConfig.api_base + path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (resp.status === 401) {
      // authorizer rejected the token (revoked/expired beyond refresh)
      this.redirectToLogin()
      return { code: -10, data: null, message: 'session expired' }
    }
    return resp.json()
  }
}

export const authStore = new AuthStore()
