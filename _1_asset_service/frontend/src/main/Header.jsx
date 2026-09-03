import { observer } from 'mobx-react-lite'
import { authStore } from './store/authStore.js'

const Header = observer(() => {
  const me = authStore.me
  return (
    <div className="header-root">
      <div className="header-title">Asset Service</div>
      <div className="header-user">
        <span className="header-user-email">{me.email || me.username}</span>
        <span className={`header-role ${me.role === 'admin' ? 'is-admin' : ''}`}>{me.role}</span>
        <button type="button" className="app-btn" onClick={authStore.logout}>
          logout
        </button>
      </div>
    </div>
  )
})

export default Header
