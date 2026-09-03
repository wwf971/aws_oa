import { observer } from 'mobx-react-lite'
import { DownIcon } from '@wwf971/react-comp-misc'
import { assetStore } from './store/assetStore.js'

const NodePanel = observer(() => {
  const node = assetStore.nodeSelected
  if (!node) {
    return <div className="node-panel-root node-panel-empty">select an item in the tree</div>
  }

  const isFolder = assetStore.getIsFolder(node)
  const typeText = isFolder
    ? 'tree folder'
    : node.asset_type === 'file' ? 'file asset' : 'folder asset'

  const rows = [
    ['type', typeText],
    ['node id', node.node_id],
  ]
  if (!isFolder) {
    rows.push(['asset id', node.asset_id])
    rows.push(['state', node.upload_state])
    if (node.size !== undefined) rows.push(['size', sizeFormat(node.size)])
    if (node.file_name) rows.push(['file name', node.file_name])
    if (node.content_type) rows.push(['content type', node.content_type])
  }
  rows.push(['created at', node.created_at])

  return (
    <div className="node-panel-root">
      <div className="node-panel-title-row">
        <div className="node-panel-title">{node.name}</div>
        {!isFolder ? (
          <button
            type="button"
            className="app-btn"
            disabled={node.upload_state !== 'ready'}
            onClick={() => assetStore.downloadRequest(node.node_id)}
          >
            <DownIcon width={12} height={12} />
            <span>download</span>
          </button>
        ) : null}
      </div>
      <div className="node-panel-rows">
        {rows.map(([key, value]) => (
          <div className="node-panel-row" key={key}>
            <div className="node-panel-row-key">{key}</div>
            <div className="node-panel-row-value">{value}</div>
          </div>
        ))}
      </div>
    </div>
  )
})

function sizeFormat(size) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`
}

export default NodePanel
