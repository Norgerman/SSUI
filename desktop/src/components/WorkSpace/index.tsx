import {useEffect, useState,useRef} from 'react';
import {Button, Tree, TreeNodeInfo, Icon, Popover, Menu, MenuItem} from "@blueprintjs/core";
import { TauriFilesystemProvider, IFilesystemProvider } from '../../providers/FilesystemProvider';
import styles from './style.module.css'

import "@blueprintjs/core/lib/css/blueprint.css";
import "@blueprintjs/icons/lib/css/blueprint-icons.css";
import PropTypes from "prop-types";

export const WorkSpace = (props) => {
    const { currentWorkspace, onOpenWorkspace, onSelectWorkflow, onFileOpen } = props
    const [ fileTree, setFileTree ] = useState([])
    const filesystemProvider = useRef<IFilesystemProvider>(props.filesystemProvider || new TauriFilesystemProvider())

    useEffect(() => {
        const fn = async () => {
            console.log('filesystemProvider.current触发')
            if (currentWorkspace) {
                const childNodes = await filesystemProvider.current.fetchFileTree(currentWorkspace, null);
                const lastword = currentWorkspace.split('/');
                setFileTree([
                    {
                        id: 0,
                        hasCaret: true,
                        label: lastword[lastword.length - 1],
                        isExpanded: true,
                        icon: "folder-close",
                        childNodes: childNodes.map(c => ({
                            ...c,
                            icon: <div className={styles.treeIcon}>{getIcon(c.id)}</div>
                        }))
                    }
                ])
            }
        }
        fn()

    }, [currentWorkspace]);

    const getIcon = (id) => {
        const extArr = id.split('.')
        const ext = extArr[extArr.length - 1]
        switch (ext) {
            case 'py':
                return (
                    <svg className="icon" viewBox="0 0 1024 1024" version="1.1"
                         xmlns="http://www.w3.org/2000/svg" p-id="5508" width="200" height="200">
                        <path d="M128 96m32 0l704 0q32 0 32 32l0 768q0 32-32 32l-704 0q-32 0-32-32l0-768q0-32 32-32Z"
                              fill="#FFFFFF" p-id="5509"></path>
                        <path
                            d="M896 832v64a32 32 0 0 1-32 32v-96h32z m-320 64v32H160a32 32 0 0 1-32-32V128a32 32 0 0 1 32-32h704a32 32 0 0 1 32 32v416h-32V128H160v768h416z"
                            fill="#5D6D7E" p-id="5510"></path>
                        <path d="M224 224h576v32H224V224z m0 128h576v32H224V352z" fill="#CDD3DA" p-id="5511"></path>
                        <path d="M224 224h352v32H224V224z m0 128h352v32H224V352z m0 128h224v32H224v-32z" fill="#ACB4C0"
                              p-id="5512"></path>
                        <path
                            d="M841.152 632.256s-28.224 70.72-73.152 72.992h-96s-62.208-19.936-68.608 46.208v59.584l-32.704 0.224s-56.416 4.8-56.416-112.032c0-58.24 56.416-59.232 56.416-59.232H736v-32h-128v-96s25.664-8.128 43.36-16.64c19.968-9.76 55.488-14.752 84.64-15.36 66.88-1.376 101.344 19.968 104.832 63.296l0.32 88.96zM640 544v32h32v-32h-32z"
                            fill="#3B97D3" p-id="5513"></path>
                        <path
                            d="M730.976 800L736 832h105.152v68.384s-0.96 61.248-110.176 59.584c-100.704-1.536-97.824-59.584-97.824-59.584v-119.104C633.152 729.824 704 736 704 736h92.576c51.936-2.144 74.24-73.92 74.24-73.92v-59.584l36 1.056c-0.032 0 53.184 3.36 53.184 115.936 0 110.048-59.424 91.52-59.424 91.52l-169.6-11.008zM768 864v32h32v-32h-32z"
                            fill="#C49F74" p-id="5514"></path>
                    </svg>
                )
            case 'yaml':
                return (
                    <svg className="icon" viewBox="0 0 1024 1024" version="1.1"
                         xmlns="http://www.w3.org/2000/svg" p-id="4454" width="160" height="160">
                        <path
                            d="M997.8 237.64L788 27.89a89.41 89.41 0 0 0-63.3-26.2H354.18c-75.08 0-136.12 61.08-136.12 136.12v333.44h59.55V137.81a76.66 76.66 0 0 1 76.57-76.57h323.4v146.58a140.32 140.32 0 0 0 140.29 140.29h146.58V885.9a76.79 76.79 0 0 1-76.57 76.56h-533.7a76.66 76.66 0 0 1-76.57-76.56v-163h-59.55v163c0 75 61 136.11 136.12 136.11h533.7A136.13 136.13 0 0 0 1024 885.9v-585a89.64 89.64 0 0 0-26.2-63.26z m-179.93 50.92a80.75 80.75 0 0 1-80.74-80.74V61.24l227.32 227.32z"
                            p-id="4455"></path>
                        <path d="M299.75 542.01l-19.71 51.29h39.04l-18.95-51.29h-0.38z" p-id="4456"></path>
                        <path
                            d="M767.57 425.24H38.28A38.28 38.28 0 0 0 0 463.53v229.69a38.28 38.28 0 0 0 38.28 38.29h729.29a38.28 38.28 0 0 0 38.28-38.29V463.53a38.28 38.28 0 0 0-38.28-38.29zM143.94 597.13v56.65h-34.83v-56.65L50.93 503h40.76l34.64 58.37h0.39L159.63 503h40.77z m197.72 56.65l-11.1-29.86h-62.2l-11.48 29.86h-36.55L280.61 503h39l58.57 150.81z m253 0h-34.79v-96.84h-0.38l-39 96.84h-33.92l-39.43-96.84h-0.38v96.84h-34.64V503h49l42.11 104.88h0.38L545.71 503h49z m160.23 0h-112.5V503H677v120.16h77.9z"
                            p-id="4457"></path>
                    </svg>
                )
        }
    }

    const handleNodeCollapse = (node: TreeNodeInfo) => {
        updateFileTreeWithChildren(node, false, undefined)
    }

    const handleNodeExpand = async (node: TreeNodeInfo) => {
        if (node.childNodes && node.childNodes.length === 0) {
            const childNodes = await filesystemProvider.current.fetchFileTree((node.nodeData as any).path as string, node);
            updateFileTreeWithChildren(node, true, childNodes);
        } else {
            updateFileTreeWithChildren(node, true, undefined);
        }
    }

    const handleNodeClick = (node: TreeNodeInfo) => {
        if (node.childNodes == undefined && props.onFileOpen) {
            props.onFileOpen((node.nodeData as any).path as string);
        }
    }

    const updateFileTreeWithChildren = (node: TreeNodeInfo, isExpanded: boolean, childNodes: TreeNodeInfo[] | undefined) => {
        const path = filesystemProvider.current.getPathToRoot(node);

        const updateChildNodes = (nodes: TreeNodeInfo[], path: string[]): TreeNodeInfo[] => {
            if (path.length === 0) return nodes;

            return nodes.map(n => {
                if (n.id === path[0]) {
                    if (path.length === 1) {
                        if (childNodes) {
                            return {...n, childNodes, isExpanded};
                        } else {
                            return {...n, isExpanded};
                        }
                    } else {
                        return {
                            ...n,
                            childNodes: n.childNodes ? updateChildNodes(n.childNodes, path.slice(1)) : n.childNodes
                        };
                    }
                }
                return n;
            });
        };

        setFileTree(updateChildNodes(fileTree, path));

    }

    return (
        <div className={styles.workspace}>
            <div className={styles.title}>
                <span className={styles.titleText}>工作空间</span>
                <span>
                    <Popover
                        content={
                            <Menu key="menu">
                                <MenuItem icon="folder-open" text="打开新的已有工作空间" onClick={onOpenWorkspace} />
                                <MenuItem icon="generate" text="从预制工作流开始" onClick={onSelectWorkflow} />
                            </Menu>
                        }
                        position="bottom-right"
                    >
                        {
                            currentWorkspace &&
                            <div className={styles.addBtn}>
                                <Icon icon="plus"></Icon>
                            </div>
                        }
                    </Popover>

                </span>
            </div>
            {currentWorkspace ? (
                <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
                    <Tree
                        contents={fileTree}
                        onNodeExpand={handleNodeExpand}
                        onNodeCollapse={handleNodeCollapse}
                        onNodeClick={handleNodeClick}
                        className={styles.tree}
                    />
                </div>
            ) : (
                <div style={{ display: 'flex', flexDirection: 'column',  padding: '20px', height: '100%' }}>
                    <p>当前没有打开的目录, 您可以：</p>
                    <div style={{ display: 'flex', justifyContent: 'center', marginTop: '10px', marginBottom: '20px' }} >
                        <Button onClick={onOpenWorkspace} icon="folder-open" size="large" variant="solid">打开已有工作空间</Button>
                    </div>
                    <p>或者，选择我们准备的预制工作流：</p>
                    <div style={{ display: 'flex', justifyContent: 'center', marginTop: '10px', marginBottom: '10px' }} >
                        <Button onClick={onSelectWorkflow} icon="generate" size="large" variant="solid">从预制工作流开始</Button>
                    </div>
                </div>
            )}
        </div>
    )
}

WorkSpace.propTypes = {
    currentWorkspace: PropTypes.string,
    onOpenWorkspace: PropTypes.func,
    onSelectWorkflow: PropTypes.func,
    onFileOpen: PropTypes.func,
    filesystemProvider: PropTypes.object
}
