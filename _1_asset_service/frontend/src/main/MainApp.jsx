import { useEffect } from 'react'
import { observer } from 'mobx-react-lite'
import { PanelDual, MessageBar, SpinningCircle } from '@wwf971/react-comp-misc'
import Header from './Header.jsx'
import TreePanel from './TreePanel.jsx'
import NodePanel from './NodePanel.jsx'
import ConfirmPopup from './ConfirmPopup.jsx'
import { authStore } from './store/authStore.js'
import { assetStore } from './store/assetStore.js'

const MainApp = observer(() => {
  useEffect(() => {
    authStore.init()
  }, [])

  useEffect(() => {
    if (authStore.initState === 'ready') {
      assetStore.treeLoad()
    }
  }, [authStore.initState])

  if (authStore.initState === 'pending') {
    return (
      <div className="app-center-root">
        <SpinningCircle width={18} height={18} />
        <div className="app-center-text">loading</div>
      </div>
    )
  }

  if (authStore.initState !== 'ready') {
    return (
      <div className="app-center-root">
        <div className="app-center-text app-center-error">
          {authStore.initState === 'no-access' ? 'no access: ' : 'error: '}
          {authStore.initErrorText}
        </div>
        <button type="button" className="app-btn" onClick={authStore.logout}>
          back to login
        </button>
      </div>
    )
  }

  return (
    <div className="app-root">
      <Header />
      <div className="app-body">
        <PanelDual initialWidth={300}>
          <div className="app-panel-left">
            <TreePanel />
          </div>
          <div className="app-panel-right">
            <NodePanel />
          </div>
        </PanelDual>
      </div>
      <MessageBar
        data={{ messageState: assetStore.messageState }}
        config={{ idleText: 'ready' }}
        onEvent={(eventType) => {
          if (eventType === 'dismissMessageRequest') {
            assetStore.messageSet('idle', '')
          }
        }}
      />
      <ConfirmPopup />
    </div>
  )
})

export default MainApp
