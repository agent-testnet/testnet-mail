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

    // Per-user aggregate severity: sum of total_severity across every
    // conversation a user participates in. Drives node size + color below
    // so the worst-affected mailboxes visually bulge and shift toward red.
    const userSeverity = new Map(users.map(u => [u, 0]));
    conversations.forEach(c => {
        const sev = Number(c.total_severity) || 0;
        if (!sev) return;
        if (userSeverity.has(c.sender))   userSeverity.set(c.sender,   userSeverity.get(c.sender)   + sev);
        if (userSeverity.has(c.receiver)) userSeverity.set(c.receiver, userSeverity.get(c.receiver) + sev);
    });
    const maxUserSeverity = Math.max(0, ...userSeverity.values());

    // Smoothly map an aggregate severity into a [0,1] intensity used by
    // both the radius and the color lerp. log1p keeps a single outlier
    // from dwarfing everyone else, and the guard avoids divide-by-zero
    // when no conversation has been scored yet (all-pending dataset).
    function severityIntensity(agg) {
        if (!agg || maxUserSeverity <= 0) return 0;
        return Math.min(1, Math.log1p(agg) / Math.log1p(maxUserSeverity));
    }

    // Green -> amber -> red along intensity 0 -> 0.5 -> 1, returned as a
    // THREE.Color via a hex int suitable for Material.color/emissive.
    function severityColor(intensity) {
        const green = new THREE.Color(0x00ff41);
        const amber = new THREE.Color(0xffaa00);
        const red   = new THREE.Color(0xff3030);
        if (intensity <= 0.5) {
            return green.clone().lerp(amber, intensity / 0.5);
        }
        return amber.clone().lerp(red, (intensity - 0.5) / 0.5);
    }

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
    // Initial dolly distance: slightly tighter than the previous R*3.2
    // so the cluster fills more of the viewport on first paint, without
    // pushing inside the controls.minDistance floor below (R*1.4).
    camera.position.set(0, 4, R * 2.6);

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

    // Each node gets its own geometry now -- radius scales per user with
    // aggregate severity, so the shared single-geo trick doesn't apply.
    // Baseline 0.5 is the original size; multiplier caps near 1.6x so the
    // worst offender bulges visibly without overwhelming the layout.
    const BASE_RADIUS = 0.5;
    const MAX_RADIUS_MULT = 1.6;

    const nodes = users.map((email, i) => {
        const agg = userSeverity.get(email) || 0;
        const intensity = severityIntensity(agg);
        const radius = BASE_RADIUS * (1 + (MAX_RADIUS_MULT - 1) * intensity);
        const color = severityColor(intensity);

        const mat = new THREE.MeshStandardMaterial({
            color: color.getHex(),
            emissive: color.getHex(),
            emissiveIntensity: 0.7,
            roughness: 0.35,
            metalness: 0.15,
        });
        const mesh = new THREE.Mesh(
            new THREE.SphereGeometry(radius, 32, 32),
            mat,
        );
        mesh.position.copy(positions[i]);
        mesh.userData = {
            email,
            type: 'node',
            baseIntensity: 0.7,
            severity: agg,
            severityIntensity: intensity,
        };
        scene.add(mesh);

        if (halos) {
            const halo = new THREE.Mesh(
                new THREE.SphereGeometry(radius * 1.8, 24, 24),
                new THREE.MeshBasicMaterial({
                    color: color.getHex(),
                    transparent: true,
                    opacity: 0.12 + intensity * 0.18,
                }),
            );
            halo.position.copy(positions[i]);
            scene.add(halo);
        }

        if (labels) {
            const label = makeLabel(email.split('@')[0]);
            label.position.copy(positions[i]).add(new THREE.Vector3(0, radius + 0.6, 0));
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
