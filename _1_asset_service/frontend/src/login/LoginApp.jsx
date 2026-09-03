// login page. intentionally tiny (no mobx, no component library): this is the
// first bundle every visitor downloads; the heavy main page lives in a
// separate build target under /main/ and is fetched only after login.
//
// this page is also the oauth callback: cognito hosted login redirects back
// here with ?code=..., which is exchanged for tokens, then we go to /main/.
import { useEffect, useState } from 'react'
import { webConfigLoad } from '../shared/webConfig.js'
import { authorizeRedirect, codeExchange } from '../shared/authApi.js'
import { tokensSave, tokensLoad, getIsTokensExpired } from '../shared/token.js'

const LoginApp = () => {
  const [phase, setPhase] = useState('loading') // loading | exchanging | ready | error
  const [errorText, setErrorText] = useState('')
  const [webConfig, setWebConfig] = useState(null)

  useEffect(() => {
    const start = async () => {
      try {
        const webConfigLoaded = await webConfigLoad()
        setWebConfig(webConfigLoaded)

        const params = new URLSearchParams(window.location.search)
        if (params.get('error')) {
          setErrorText(`login rejected: ${params.get('error_description') || params.get('error')}`)
          setPhase('error')
          return
        }
        const code = params.get('code')
        if (code) {
          setPhase('exchanging')
          const tokenResponse = await codeExchange(webConfigLoaded, code)
          tokensSave(tokenResponse)
          window.location.replace('/main/')
          return
        }
        const tokens = tokensLoad()
        if (tokens && !getIsTokensExpired(tokens)) {
          window.location.replace('/main/')
          return
        }
        setPhase('ready')
      } catch (error) {
        setErrorText(String(error.message || error))
        setPhase('error')
      }
    }
    start()
  }, [])

  return (
    <div className="login-root">
      <div className="login-card">
        <div className="login-title">Asset Service</div>
        <div className="login-subtitle">a simple web drive on aws</div>

        {phase === 'loading' ? <div className="login-status">loading...</div> : null}
        {phase === 'exchanging' ? <div className="login-status">completing login...</div> : null}

        {phase === 'ready' || phase === 'error' ? (
          <button
            type="button"
            className="login-signin-btn"
            onClick={() => authorizeRedirect(webConfig)}
            disabled={!webConfig}
          >
            Sign in with Cognito
          </button>
        ) : null}

        {phase === 'error' ? <div className="login-error">{errorText}</div> : null}
      </div>
    </div>
  )
}

export default LoginApp
