import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

function makeLabel(text) {
    const c = document.createElement('canvas');
    c.width = 256; c.height = 64;
    const cx = c.getContext('2d');
    cx.font = 'bold 30px "Courier New", monospace';
    cx.textAlign = 'center';
    cx.textBaseline = 'middle';
    cx.shadowColor = '#00ff41';
    cx.shadowBlur = 14;
    cx.fillStyle = '#00ff41';
    cx.fillText(text, 128, 32);
    const tex = new THREE.CanvasTexture(c);
    const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, depthWrite: false });
    const sprite = new THREE.Sprite(mat);
    sprite.scale.set(2.4, 0.6, 1);
    return sprite;
}

export function buildNetworkScene(canvas, conversations, options = {}) {
    const {
        useOrbitControls = true,
        autoRotate = true,
        labels = true,
        halos = true,
    } = options;

    const userSet = new Set();
    conversations.forEach(c => { userSet.add(c.sender); userSet.add(c.receiver); });
    const users = Array.from(userSet).sort();
    const userIdx = new Map(users.map((u, i) => [u, i]));

    const R = Math.max(6, users.length * 1.2);
    const positions = users.map((_, i) => {
        const n = users.length;
        const y = 1 - (i / Math.max(1, n - 1)) * 2;
        const radius = Math.sqrt(1 - y * y);
        const theta = Math.PI * (1 + Math.sqrt(5)) * i;
        return new THREE.Vector3(
            R * Math.cos(theta) * radius,
            R * y,
            R * Math.sin(theta) * radius
        );
    });
    if (users.length === 1) positions[0].set(0, 0, 0);
    if (users.length === 2) { positions[0].set(-R, 0, 0); positions[1].set(R, 0, 0); }

    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x000000, 0.025);

    const camera = new THREE.PerspectiveCamera(
        60,
        canvas.clientWidth / Math.max(1, canvas.clientHeight),
        0.1,
        500
    );
    camera.position.set(0, 4, R * 3.2);

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    renderer.setClearColor(0x000000, 0);
    renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);

    let controls = null;
    if (useOrbitControls) {
        controls = new OrbitControls(camera, canvas);
        controls.enableDamping = true;
        controls.dampingFactor = 0.08;
        controls.autoRotate = autoRotate;
        controls.autoRotateSpeed = 0.4;
        controls.minDistance = R * 1.4;
        controls.maxDistance = R * 6;
    }

    scene.add(new THREE.AmbientLight(0x224422, 0.5));
    const keyLight = new THREE.PointLight(0x00ff41, 2.0, 200);
    keyLight.position.set(R, R, R);
    scene.add(keyLight);
    const fillLight = new THREE.PointLight(0x008822, 1.2, 200);
    fillLight.position.set(-R, -R * 0.5, R);
    scene.add(fillLight);

    const nodeGeo = new THREE.SphereGeometry(0.5, 32, 32);
    const nodes = users.map((email, i) => {
        const mat = new THREE.MeshStandardMaterial({
            color: 0x00ff41,
            emissive: 0x00ff41,
            emissiveIntensity: 0.7,
            roughness: 0.35,
            metalness: 0.15,
        });
        const mesh = new THREE.Mesh(nodeGeo, mat);
        mesh.position.copy(positions[i]);
        mesh.userData = { email, type: 'node', baseIntensity: 0.7 };
        scene.add(mesh);

        if (halos) {
            const halo = new THREE.Mesh(
                new THREE.SphereGeometry(0.9, 24, 24),
                new THREE.MeshBasicMaterial({ color: 0x00ff41, transparent: true, opacity: 0.12 })
            );
            halo.position.copy(positions[i]);
            scene.add(halo);
        }

        if (labels) {
            const label = makeLabel(email.split('@')[0]);
            label.position.copy(positions[i]).add(new THREE.Vector3(0, 1.1, 0));
            scene.add(label);
        }

        return mesh;
    });

    const edges = conversations.map(conv => {
        const a = userIdx.get(conv.sender);
        const b = userIdx.get(conv.receiver);
        if (a == null || b == null) return null;
        const points = [positions[a], positions[b]];
        const geom = new THREE.BufferGeometry().setFromPoints(points);
        const intensity = Math.min(1, 0.25 + Math.log(conv.message_count + 1) * 0.28);
        const mat = new THREE.LineBasicMaterial({
            color: 0x00ff41,
            transparent: true,
            opacity: intensity,
        });
        const line = new THREE.Line(geom, mat);
        line.userData = { conv, baseOpacity: intensity, type: 'edge' };
        scene.add(line);
        return line;
    }).filter(Boolean);

    let rafId = null;
    let externalTick = null;
    function animate() {
        rafId = requestAnimationFrame(animate);
        if (externalTick) externalTick();
        if (controls) controls.update();
        renderer.render(scene, camera);
    }
    function start() { if (!rafId) animate(); }
    function stop() { if (rafId) { cancelAnimationFrame(rafId); rafId = null; } }
    function setTick(fn) { externalTick = fn; }

    function fit() {
        const w = canvas.clientWidth, h = canvas.clientHeight;
        if (!w || !h) return;
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
    }
    new ResizeObserver(fit).observe(canvas);
    window.addEventListener('resize', fit);
    fit();

    return {
        THREE, scene, camera, renderer, controls,
        nodes, edges, positions, users, userIdx, R,
        start, stop, setTick, fit,
    };
}
