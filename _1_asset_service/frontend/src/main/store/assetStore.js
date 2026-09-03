// source of truth of the main page: tree nodes fetched from the api, plus all
// ui operation state (selection, expand, rename edit, upload progress,
// delete confirm, message bar). components render from here and send change
// attempts back here; this store talks to the server and accepts/rejects.
import { makeAutoObservable, runInAction } from 'mobx'
import { authStore } from './authStore.js'
import { rankBetween } from '../../shared/lexorank.js'

const FOLDER_NAME_DEFAULT = 'new-folder'

class AssetStore {
  nodeById = {}
  nodeSelectedId = null
  expandedById = {}
  isTreeLoading = false

  renameEditId = null
  isRenameSubmitting = false

  // { name, fileDone, fileTotal } while an upload is running
  uploadState = null

  // { nodeId, name } while the delete confirm popup is open
  confirmDelete = null

  messageState = { status: 'idle', messageText: '' }

  constructor() {
    makeAutoObservable(this, {}, { autoBind: true })
  }

  get nodeSelected() {
    return this.nodeSelectedId ? this.nodeById[this.nodeSelectedId] || null : null
  }

  messageSet(status, messageText) {
    this.messageState = { status, messageText }
  }

  // ------------------------------------------------------------- tree shape

  getIsFolder(node) {
    return !node.asset_id
  }

  childIdsGet(parentId) {
    const children = Object.values(this.nodeById).filter((node) => {
      const nodeParentId = node.parent_id ?? null
      return nodeParentId === parentId
    })
    children.sort((nodeA, nodeB) => {
      if (nodeA.lexorank !== nodeB.lexorank) return nodeA.lexorank < nodeB.lexorank ? -1 : 1
      return nodeA.name < nodeB.name ? -1 : 1
    })
    return children.map((node) => node.node_id)
  }

  // data prop for TreeView
  treeDataBuild() {
    const itemDataById = {}
    for (const node of Object.values(this.nodeById)) {
      const isFolder = this.getIsFolder(node)
      const suffix = node.upload_state === 'pending' ? ' (uploading)' : ''
      itemDataById[node.node_id] = {
        id: node.node_id,
        text: `${node.name}${suffix}`,
        isLeaf: !isFolder,
        isExpanded: this.expandedById[node.node_id] === true,
        childrenIds: isFolder ? this.childIdsGet(node.node_id) : [],
        childrenLoadState: 'loaded',
        node,
      }
    }
    return {
      itemRootIds: this.childIdsGet(null),
      itemDataById,
      itemSelectedId: this.nodeSelectedId,
    }
  }

  // folder that new folders/uploads go into, based on current selection:
  // selected folder itself, or the parent of a selected asset, or root
  folderTargetIdGet() {
    const node = this.nodeSelected
    if (!node) return null
    if (this.getIsFolder(node)) return node.node_id
    return node.parent_id ?? null
  }

  // ------------------------------------------------------------ tree loading

  async treeLoad() {
    this.isTreeLoading = true
    const resp = await authStore.apiCall('GET', '/tree')
    runInAction(() => {
      this.isTreeLoading = false
      if (resp.code !== 0) {
        this.messageSet('error', `failed to load tree: ${resp.message}`)
        return
      }
      const nodeById = {}
      for (const node of resp.data.nodes) {
        nodeById[node.node_id] = node
      }
      this.nodeById = nodeById
    })
  }

  // ---------------------------------------------------------------- selection

  nodeSelect(nodeId) {
    this.nodeSelectedId = nodeId
  }

  expandToggle(nodeId, nextIsExpanded) {
    this.expandedById[nodeId] = nextIsExpanded
  }

  // ------------------------------------------------------------------ folder

  async folderCreate() {
    const parentId = this.folderTargetIdGet()
    const resp = await authStore.apiCall('POST', '/folder', {
      name: FOLDER_NAME_DEFAULT,
      parent_id: parentId,
    })
    if (resp.code !== 0) {
      this.messageSet('error', `failed to create folder: ${resp.message}`)
      return
    }
    runInAction(() => {
      const node = resp.data.node
      this.nodeById[node.node_id] = node
      if (parentId) this.expandedById[parentId] = true
      this.nodeSelectedId = node.node_id
      this.renameEditId = node.node_id
    })
  }

  // ------------------------------------------------------------------ rename

  renameStart(nodeId) {
    this.renameEditId = nodeId
  }

  renameCancel() {
    this.renameEditId = null
  }

  async renameSubmit(nodeId, name) {
    const nameTrimmed = (name || '').trim()
    const node = this.nodeById[nodeId]
    if (!node || this.isRenameSubmitting) return
    if (!nameTrimmed || nameTrimmed === node.name) {
      this.renameEditId = null
      return
    }
    this.isRenameSubmitting = true
    const resp = await authStore.apiCall('PATCH', `/node/${nodeId}`, { name: nameTrimmed })
    runInAction(() => {
      this.isRenameSubmitting = false
      this.renameEditId = null
      if (resp.code !== 0) {
        this.messageSet('error', `failed to rename: ${resp.message}`)
        return
      }
      node.name = nameTrimmed
    })
  }

  // ------------------------------------------------------------------ upload

  // fileList comes from a hidden <input type=file>; for folder upload the
  // input has webkitdirectory, and every file carries webkitRelativePath
  // like "topFolder/xx/yy.txt"
  async assetUpload(fileList, isFolderUpload) {
    const files = Array.from(fileList)
    if (files.length === 0) return

    let assetName
    let fileEntries
    if (isFolderUpload) {
      assetName = files[0].webkitRelativePath.split('/')[0]
      fileEntries = files.map((file) => ({
        path: file.webkitRelativePath.split('/').slice(1).join('/'),
        size: file.size,
        content_type: file.type || 'application/octet-stream',
      }))
    } else {
      assetName = files[0].name
      fileEntries = [{
        path: files[0].name,
        size: files[0].size,
        content_type: files[0].type || 'application/octet-stream',
      }]
    }

    const parentId = this.folderTargetIdGet()
    this.uploadState = { name: assetName, fileDone: 0, fileTotal: files.length }
    this.messageSet('loading', `uploading ${assetName}...`)

    const respCreate = await authStore.apiCall('POST', '/asset', {
      name: assetName,
      parent_id: parentId,
      asset_type: isFolderUpload ? 'folder' : 'file',
      files: fileEntries,
    })
    if (respCreate.code !== 0) {
      runInAction(() => {
        this.uploadState = null
        this.messageSet('error', `failed to start upload: ${respCreate.message}`)
      })
      return
    }
    const node = respCreate.data.node
    runInAction(() => {
      this.nodeById[node.node_id] = node
      if (parentId) this.expandedById[parentId] = true
    })

    const fileByPath = {}
    files.forEach((file, index) => {
      fileByPath[fileEntries[index].path] = file
    })
    for (const uploadUrl of respCreate.data.upload_urls) {
      const file = fileByPath[uploadUrl.path]
      const respPut = await fetch(uploadUrl.url, {
        method: 'PUT',
        headers: uploadUrl.headers,
        body: file,
      })
      if (!respPut.ok) {
        runInAction(() => {
          this.uploadState = null
          this.messageSet('error', `upload failed for ${uploadUrl.path} (http ${respPut.status})`)
        })
        return
      }
      runInAction(() => {
        this.uploadState.fileDone += 1
        this.messageSet(
          'loading',
          `uploading ${assetName}: ${this.uploadState.fileDone}/${this.uploadState.fileTotal}`,
        )
      })
    }

    const respComplete = await authStore.apiCall('POST', '/asset-complete', {
      node_id: node.node_id,
    })
    runInAction(() => {
      this.uploadState = null
      if (respComplete.code !== 0) {
        this.messageSet('error', `failed to complete upload: ${respComplete.message}`)
        return
      }
      this.nodeById[node.node_id] = respComplete.data.node
      this.messageSet('success', `uploaded ${assetName}`)
    })
  }

  // -------------------------------------------------------------------- move

  // drop comes from TreeView 'moveItem' event:
  // { type, itemParentId, itemBeforeId, itemAfterId }
  async nodeMoveByDrop(nodeId, drop) {
    const parentId = drop.itemParentId ?? null
    let rankPrev
    let rankNext
    if (drop.type === 'under') {
      // dropped onto a folder: place after its current last child
      const childIds = this.childIdsGet(parentId).filter((childId) => childId !== nodeId)
      const childLastId = childIds[childIds.length - 1]
      rankPrev = childLastId ? this.nodeById[childLastId].lexorank : null
      rankNext = null
    } else {
      rankPrev = drop.itemBeforeId ? this.nodeById[drop.itemBeforeId].lexorank : null
      rankNext = drop.itemAfterId ? this.nodeById[drop.itemAfterId].lexorank : null
    }
    const lexorank = rankBetween(rankPrev, rankNext)

    const resp = await authStore.apiCall('PATCH', `/node/${nodeId}`, {
      parent_id: parentId,
      lexorank,
    })
    if (resp.code !== 0) {
      this.messageSet('error', `failed to move: ${resp.message}`)
      return
    }
    runInAction(() => {
      const node = this.nodeById[nodeId]
      node.lexorank = lexorank
      if (parentId === null) {
        delete node.parent_id
      } else {
        node.parent_id = parentId
        this.expandedById[parentId] = true
      }
    })
  }

  // ------------------------------------------------------------------ delete

  deleteRequest(nodeId) {
    const node = this.nodeById[nodeId]
    if (!node) return
    this.confirmDelete = { nodeId, name: node.name }
  }

  deleteCancel() {
    this.confirmDelete = null
  }

  async deleteConfirm() {
    const { nodeId, name } = this.confirmDelete
    this.confirmDelete = null
    const resp = await authStore.apiCall('DELETE', `/node/${nodeId}`)
    if (resp.code !== 0) {
      this.messageSet('error', `failed to delete: ${resp.message}`)
      return
    }
    runInAction(() => {
      for (const nodeIdDeleted of resp.data.node_ids_deleted) {
        delete this.nodeById[nodeIdDeleted]
      }
      if (!this.nodeById[this.nodeSelectedId]) {
        this.nodeSelectedId = null
      }
      this.messageSet('success', `deleted ${name}`)
    })
  }

  // ---------------------------------------------------------------- download

  async downloadRequest(nodeId) {
    const node = this.nodeById[nodeId]
    if (!node) return
    // folder assets are zipped inside lambda, which can take a while
    this.messageSet('loading', `preparing download of ${node.name}...`)
    const resp = await authStore.apiCall('GET', `/download/${nodeId}`)
    if (resp.code !== 0) {
      this.messageSet('error', `failed to download: ${resp.message}`)
      return
    }
    const anchor = document.createElement('a')
    anchor.href = resp.data.url
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    this.messageSet('success', `download started: ${node.name}`)
  }
}

export const assetStore = new AssetStore()
