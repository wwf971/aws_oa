import { observer } from 'mobx-react-lite'
import { assetStore } from './store/assetStore.js'

// delete confirm popup (browser confirm() is not used in this project)
const ConfirmPopup = observer(() => {
  const confirmDelete = assetStore.confirmDelete
  if (!confirmDelete) return null

  return (
    <div className="confirm-popup-backdrop" onClick={() => assetStore.deleteCancel()}>
      <div className="confirm-popup-card" onClick={(event) => event.stopPropagation()}>
        <div className="confirm-popup-text">
          Delete "{confirmDelete.name}"? Everything under it will be deleted.
        </div>
        <div className="confirm-popup-btn-row">
          <button type="button" className="app-btn" onClick={() => assetStore.deleteCancel()}>
            cancel
          </button>
          <button
            type="button"
            className="app-btn app-btn-danger"
            onClick={() => assetStore.deleteConfirm()}
          >
            delete
          </button>
        </div>
      </div>
    </div>
  )
})

export default ConfirmPopup
