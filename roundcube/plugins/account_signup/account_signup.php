<?php

class account_signup extends rcube_plugin
{
    function init()
    {
        $this->add_texts('localization/', false);
        $this->add_hook('render_page', array($this, 'render_page'));
    }

    function render_page($args)
    {
        if ($args['template'] !== 'login') {
            return $args;
        }

        $label = rcube::Q($this->gettext('create_account'));
        $script = <<<JS
<script>
(function(){
  var btn = document.querySelector('#rcmloginsubmit');
  if (!btn) return;
  var p = btn.closest('.formbuttons') || btn.parentNode;
  var d = document.createElement('div');
  d.style.cssText = 'text-align:center;padding-top:0.8em';
  var a = document.createElement('a');
  a.href = '/signup';
  a.style.cssText = 'color:#4285f4;font-size:0.9em;text-decoration:none';
  a.textContent = '{$label}';
  d.appendChild(a);
  p.parentNode.insertBefore(d, p.nextSibling);
})();
</script>
JS;

        $args['content'] = str_replace('</body>', $script . '</body>', $args['content']);
        return $args;
    }
}
