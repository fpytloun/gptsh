import os
import tempfile
from gptsh.config import loader

def test_env_expansion_and_merge():
    # Write temp configs
    with tempfile.TemporaryDirectory() as tmp:
        global_cfg = os.path.join(tmp, 'global.yml')
        project_cfg = os.path.join(tmp, 'proj.yml')
        with open(global_cfg, 'w') as f:
            f.write('a: 1\nb: ${TEST_B}\nsub: { x: foo }\n')
        with open(project_cfg, 'w') as f:
            f.write('b: 2\nsub: { y: bar }\n')
        os.environ['TEST_B'] = 'envb'
        config = loader.load_config(paths=[global_cfg, project_cfg])
        assert config['a'] == 1
        assert config['b'] == 'envb'
        assert config['sub']['x'] == 'foo'
        assert config['sub']['y'] == 'bar'
