import { useEffect, useRef, useState } from 'react'
import { observer } from 'mobx-react-lite'
import {
  TreeView,
  MenuComp,
  AddIcon,
  UploadIcon,
  FolderIcon,
  RefreshClockwise,
  SpinningCircle,
} from '@wwf971/react-comp-misc'
import { authStore } from './store/authStore.js'
import { assetStore } from './store/assetStore.js'

const TreePanel = observer(() => {
  const inputFileRef = useRef(null)
  const inputFolderRef = useRef(null)
  // { nodeId, position: {x, y} } while the right-click menu is open
  const [menuState, setMenuState] = useState(null)

  const isAdmin = authStore.isAdmin
  const treeData = assetStore.treeDataBuild()
  const menuNode = menuState ? assetStore.nodeById[menuState.nodeId] : null

  return (
    <div className="tree-panel-root">
      <div className="tree-panel-toolbar">
        {isAdmin ? (
          <>
            <button type="button" className="app-btn" onClick={() => assetStore.folderCreate()}>
              <AddIcon width={12} height={12} />
              <span>new folder</span>
            </button>
            <button type="button" className="app-btn" onClick={() => inputFileRef.current.click()}>
              <UploadIcon width={12} height={12} />
              <span>upload file</span>
            </button>
            <button type="button" className="app-btn" onClick={() => inputFolderRef.current.click()}>
              <FolderIcon width={12} height={12} />
              <span>upload folder</span>
            </button>
          </>
        ) : null}
        <button type="button" className="app-btn" onClick={() => assetStore.treeLoad()}>
          <RefreshClockwise width={12} height={12} />
          <span>refresh</span>
        </button>
        {assetStore.isTreeLoading ? <SpinningCircle width={13} height={13} /> : null}
      </div>

      <input
        ref={inputFileRef}
        type="file"
        className="tree-panel-hidden-input"
        onChange={(event) => {
          assetStore.assetUpload(event.target.files, false)
          event.target.value = ''
        }}
      />
      <input
        ref={inputFolderRef}
        type="file"
        webkitdirectory=""
        className="tree-panel-hidden-input"
        onChange={(event) => {
          assetStore.assetUpload(event.target.files, true)
          event.target.value = ''
        }}
      />

      <div className="tree-panel-tree">
        <TreeView
          data={treeData}
          config={{
            isItemDragEnabled: isAdmin,
            getItemComp: (itemData) =>
              itemData.id === assetStore.renameEditId ? TreeItemRename : null,
          }}
          onEvent={async (eventType, eventData) => {
            if (eventType === 'itemClick') {
              assetStore.nodeSelect(eventData.itemId)
              return { code: 0 }
            }
            if (eventType === 'toggleExpand') {
              assetStore.expandToggle(eventData.itemId, eventData.nextIsExpanded)
              return { code: 0 }
            }
            if (eventType === 'itemContextMenu') {
              assetStore.nodeSelect(eventData.itemId)
              setMenuState({
                nodeId: eventData.itemId,
                position: { x: eventData.event.clientX, y: eventData.event.clientY },
              })
              return { code: 0 }
            }
            if (eventType === 'moveItem') {
              await assetStore.nodeMoveByDrop(eventData.itemId, eventData.drop)
              return { code: 0 }
            }
            return { code: 0 }
          }}
        />
        {treeData.itemRootIds.length === 0 && !assetStore.isTreeLoading ? (
          <div className="tree-panel-empty">
            no items yet{isAdmin ? ', create a folder or upload something' : ''}
          </div>
        ) : null}
      </div>

      {menuState && menuNode ? (
        <MenuComp
          data={{
            items: [
              {
                id: 'download',
                label: 'Download',
                isDisabled: assetStore.getIsFolder(menuNode) || menuNode.upload_state !== 'ready',
              },
              { id: 'rename', label: 'Rename', isDisabled: !isAdmin },
              { id: 'delete', label: 'Delete', isDisabled: !isAdmin },
            ],
          }}
          config={{ posOpen: menuState.position, minWidth: 120 }}
          onEvent={(eventType, eventData) => {
            if (eventType === 'closeRequest' || eventType === 'backdropClick') {
              setMenuState(null)
              return
            }
            if (eventType === 'itemClick') {
              const nodeId = menuState.nodeId
              setMenuState(null)
              if (eventData.itemId === 'download') assetStore.downloadRequest(nodeId)
              if (eventData.itemId === 'rename') assetStore.renameStart(nodeId)
              if (eventData.itemId === 'delete') assetStore.deleteRequest(nodeId)
            }
          }}
        />
      ) : null}
    </div>
  )
})

// in-place rename: the tree item text switches to contenteditable
const TreeItemRename = observer(({ itemData }) => {
  const editRef = useRef(null)
  const nodeId = itemData.id

  useEffect(() => {
    const el = editRef.current
    if (!el) return
    el.textContent = assetStore.nodeById[nodeId]?.name || ''
    el.focus()
    const range = document.createRange()
    range.selectNodeContents(el)
    const selection = window.getSelection()
    selection.removeAllRanges()
    selection.addRange(range)
  }, [nodeId])

  return (
    <span className="tree-rename-root" onClick={(event) => event.stopPropagation()}>
      <span
        ref={editRef}
        className="tree-rename-text"
        contentEditable={!assetStore.isRenameSubmitting}
        suppressContentEditableWarning={true}
        onBlur={(event) => assetStore.renameSubmit(nodeId, event.currentTarget.textContent)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.preventDefault()
            assetStore.renameCancel()
          }
          if (event.nativeEvent.isComposing) return
          if (event.key === 'Enter') {
            event.preventDefault()
            assetStore.renameSubmit(nodeId, event.currentTarget.textContent)
          }
        }}
      />
      {assetStore.isRenameSubmitting ? <SpinningCircle width={12} height={12} /> : null}
    </span>
  )
})

export default TreePanel
