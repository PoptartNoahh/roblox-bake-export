# roblox-bake-export

A small Blender addon. Path-traces lighting into a per-object albedo texture and exports a Roblox-ready FBX. Drop the FBX into Roblox Studio's 3D Importer and meshes come in pre-lit.

![preview](./docs/preview.png)

## Install

1. Download [`roblox_blender_bake.py`](./roblox_blender_bake.py).
2. In Blender: `Edit > Preferences > Add-ons > Install...`, pick the file, enable the checkbox.
3. Press `N` in the 3D View, open the **Roblox Bake** tab.

Tested on Blender 3.6 and 4.x.

## Use

1. Build a scene. Meshes, lights, optional flat colors or base-color textures on the Principled BSDF.
2. Save the `.blend` (or set an absolute output folder in the panel).
3. Select the meshes to bake, or leave nothing selected to bake all visible meshes.
4. Click **Bake & Export FBX**.

Output:

```
<output_folder>/
  <SceneName>.fbx              # textures embedded, ready for Roblox
  textures/
    <ObjName>_Albedo.png       # one per object
```

Shortcut: `Ctrl+Shift+Alt+B` in the 3D View.

## Summary

- For each mesh: auto-unwraps if it has no UVs (or zero-area UVs), creates a blank albedo image, runs a Cycles `COMBINED` bake, writes `<ObjName>_Albedo.png`.
- Builds a temporary Principled BSDF (Roughness 1, Metallic 0, Specular 0) wired to the baked image, so the FBX exporter writes it as the diffuse texture.
- Exports FBX with `path_mode='COPY'` + `embed_textures=True`, Y-up, `-Z` forward, mesh-only, face smoothing.

v1 is albedo-only. No SurfaceAppearance / Normal / Roughness / Metalness maps, no atlasing, no stud-scale conversion (1 Blender unit = 1 stud). Auto-unwrap rebuilds the UV layout, so existing tiled UVs on un-UV'd meshes will not survive - either UV-unwrap manually first or accept the new layout.

## License

GPL-3.0-or-later. Blender addons inherit Blender's license. See [LICENSE](./LICENSE).
