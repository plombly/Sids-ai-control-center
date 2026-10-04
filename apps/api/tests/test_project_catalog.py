"""Build recipes (project_catalog.py)."""

import project_catalog



def test_android_builds_fit_gradle_into_the_build_memory(tmp_path):
    import subprocess
    (tmp_path / "android").mkdir()
    (tmp_path / "android" / "gradle.properties").write_text(
        "org.gradle.jvmargs=-Xmx8G -XX:MaxMetaspaceSize=4G -XX:ReservedCodeCacheSize=512m\nandroid.useAndroidX=true\n")
    subprocess.run(["sh", "-c", project_catalog.GRADLE_FIT + "true"], cwd=tmp_path, check=True)
    text = (tmp_path / "android" / "gradle.properties").read_text()
    assert "-Xmx2G" in text and "MaxMetaspaceSize=768m" in text and "android.useAndroidX=true" in text
    for recipe in ("flutter", "android_gradle"):
        assert project_catalog.RECIPES[recipe]["command"].startswith(project_catalog.GRADLE_FIT)
